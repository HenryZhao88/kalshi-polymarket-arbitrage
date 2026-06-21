"""Storage round-trip tests against in-memory SQLite."""

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arb_scanner.app.markets.discovery import ManualReviewSort, MatchedPair, evaluate_pair
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.export import EXPORT_FIELDS, NOT_TRADE_SAFE_LABEL
from arb_scanner.app.storage.repo import (
    OpportunityRepo,
    PairRepo,
    SnapshotRepo,
    SqlAlchemyScanStore,
)
from arb_scanner.app.storage.reporting import (
    render_pair_row,
    run_diagnostic_report,
    run_retention_cleanup,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    factory = make_session_factory(engine)
    async with factory() as sess:
        yield sess
    await engine.dispose()


PAIR = MatchedPair(
    kalshi_ticker="KXBTCD-26JUN30-T70000",
    kalshi_title="Bitcoin above $70,000 on June 30?",
    poly_condition_id="0xabc",
    poly_question="Will BTC be above $70k on June 30?",
    poly_yes_token_id="111",
    poly_no_token_id="222",
    confidence=0.93,
    status=MatchStatus.ACCEPTED,
    matched_fields={"similarity_stage": "structured"},
    differing_fields={},
    rule_warnings=("UMA challenge window",),
)


class TestPairRepo:
    async def test_round_trip(self, session: AsyncSession) -> None:
        repo = PairRepo(session)
        row = await repo.add(PAIR)
        assert row.id is not None
        accepted = await repo.list_by_status("accepted")
        assert len(accepted) == 1
        assert accepted[0].kalshi_ticker == PAIR.kalshi_ticker
        assert accepted[0].rule_warnings == ["UMA challenge window"]
        assert accepted[0].matched_fields["kalshi_title"].startswith("Bitcoin")

    async def test_filtered_by_status(self, session: AsyncSession) -> None:
        repo = PairRepo(session)
        await repo.add(PAIR)
        assert await repo.list_by_status("rejected") == []

    async def test_latest_scan_and_diagnostic_rendering(self, session: AsyncSession) -> None:
        repo = PairRepo(session)
        await repo.add(replace(PAIR, status=MatchStatus.REJECTED, scan_id="old"))
        latest = replace(
            PAIR,
            status=MatchStatus.MANUAL_REVIEW,
            scan_id="new",
            missing_rule_fields=("void_policy",),
            status_reasons=("void policy unknown",),
            fee_confidence="market_metadata",
        )
        await repo.add(latest)
        rows = await repo.list_latest_scan(limit=20)
        assert len(rows) == 1
        rendered = "\n".join(render_pair_row(rows[0]))
        assert "MANUAL_REVIEW" in rendered
        assert "NOT TRADE SAFE" in rendered
        assert "void_policy" in rendered

    async def test_raw_candidate_persistence_is_disabled_by_default(
        self, session: AsyncSession
    ) -> None:
        store = SqlAlchemyScanStore(session)
        raw = replace(
            PAIR,
            status=MatchStatus.REJECTED,
            status_reasons=("similarity below structured-review threshold",),
        )
        assert await store.add_pair(raw) is None
        assert await PairRepo(session).list_by_status("rejected") == []

    async def test_rejected_candidate_cap_is_applied(self, session: AsyncSession) -> None:
        store = SqlAlchemyScanStore(session, persist_raw_candidates=True, max_candidates_per_scan=1)
        first = replace(
            PAIR,
            status=MatchStatus.REJECTED,
            status_reasons=("market type conflict",),
        )
        second = replace(first, poly_condition_id="0xsecond")
        assert await store.add_pair(first) is not None
        assert await store.add_pair(second) is None
        assert len(await PairRepo(session).list_by_status("rejected")) == 1

    async def test_retention_removes_expired_pairs(self, session: AsyncSession) -> None:
        store = SqlAlchemyScanStore(session)
        row = await PairRepo(session).add(PAIR)
        row.created_at = datetime.now(UTC) - timedelta(days=60)
        await session.flush()
        await store.apply_retention(datetime.now(UTC) - timedelta(days=30))
        assert await PairRepo(session).list_by_status("accepted") == []


def _seed_manual_review(database_url: str, *, aged_days: int | None = None) -> None:
    async def run() -> None:
        engine = make_engine(database_url)
        await init_models(engine)
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = PairRepo(session)
            complete = replace(
                PAIR,
                status=MatchStatus.MANUAL_REVIEW,
                scan_id="scan-1",
                confidence=0.85,
                missing_rule_fields=("void_policy",),
                status_reasons=("void policy unknown",),
            )
            incomplete = replace(
                complete,
                poly_condition_id="0xincomplete",
                confidence=0.95,
                missing_rule_fields=("void_policy", "resolution_source"),
            )
            row = await repo.add(complete)
            await repo.add(incomplete)
            if aged_days is not None:
                row.created_at = datetime.now(UTC) - timedelta(days=aged_days)
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


class TestDiagnosticReportFormatsEndToEnd:
    """run_diagnostic_report against a real SQLite file, all output formats."""

    @pytest.fixture
    def database_url(self, tmp_path: Path) -> str:
        url = f"sqlite+aiosqlite:///{tmp_path}/report.db"
        _seed_manual_review(url)
        return url

    def test_text_to_stdout_default(
        self, database_url: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run_diagnostic_report(
                database_url, mode="manual_review", limit=10, sort=ManualReviewSort.SIMILARITY
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "persisted candidate report: mode=manual_review" in out
        assert "NOT TRADE SAFE" in out
        assert "verify manually:" in out

    def test_csv_and_json_files_share_sorted_order(
        self, database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        csv_path = tmp_path / "manual_review.csv"
        json_path = tmp_path / "manual_review.json"
        for fmt, path in (("csv", csv_path), ("json", json_path)):
            assert (
                run_diagnostic_report(
                    database_url,
                    mode="manual_review",
                    limit=10,
                    sort=ManualReviewSort.MISSING_FIELDS,
                    fmt=fmt,
                    output=str(path),
                )
                == 0
            )
            assert f"wrote 2 manual-review row(s) to {path}" in capsys.readouterr().out
            assert path.exists()
        csv_rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
        payload = json.loads(json_path.read_text())
        csv_order = [row["polymarket_condition_id"] for row in csv_rows]
        json_order = [row["polymarket_condition_id"] for row in payload["rows"]]
        # missing_fields sort: fewer unresolved fields first, in every format.
        assert csv_order == json_order == ["0xabc", "0xincomplete"]
        assert list(csv_rows[0]) == list(EXPORT_FIELDS)
        assert payload["label"] == NOT_TRADE_SAFE_LABEL
        assert all(row["not_trade_safe_label"] == NOT_TRADE_SAFE_LABEL for row in payload["rows"])

    def test_verification_packet_labels_every_row(
        self, database_url: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run_diagnostic_report(
                database_url,
                mode="manual_review",
                limit=10,
                sort=ManualReviewSort.MISSING_FIELDS,
                verification_packet=True,
            )
            == 0
        )
        out = capsys.readouterr().out
        assert out.count(NOT_TRADE_SAFE_LABEL) >= 3  # header + both rows
        assert "verify manually before trusting this match:" in out
        assert "no claim of arbitrage or profitability" in out

    def test_empty_database_reports_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        url = f"sqlite+aiosqlite:///{tmp_path}/empty.db"
        assert run_diagnostic_report(url, mode="manual_review", limit=10) == 1
        assert "no persisted manual-review candidates" in capsys.readouterr().out


class TestRetentionCleanupCommand:
    def test_cleanup_removes_only_expired_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        url = f"sqlite+aiosqlite:///{tmp_path}/retention.db"
        _seed_manual_review(url, aged_days=60)
        assert run_retention_cleanup(url, retention_days=30) == 0
        out = capsys.readouterr().out
        assert "retention cleanup: removed rows older than 30d" in out
        assert "matched_pairs=1" in out

        async def count() -> int:
            engine = make_engine(url)
            factory = make_session_factory(engine)
            async with factory() as session:
                rows = await PairRepo(session).list_by_status("manual_review")
            await engine.dispose()
            return len(rows)

        assert asyncio.run(count()) == 1


class TestSnapshotAndOpportunity:
    async def test_snapshot_round_trip(self, session: AsyncSession) -> None:
        snap = await SnapshotRepo(session).add("kalshi", "TICKER", {"yes_dollars": []})
        assert snap.id is not None and snap.captured_at is not None

    async def test_opportunity_round_trip(self, session: AsyncSession) -> None:
        repo = OpportunityRepo(session)
        await repo.add(
            pair_id=None,
            direction="kalshi_yes_poly_no",
            size="100",
            gross_micros=7_000_000,
            net_micros=6_253_600,
            fee_breakdown={"kalshi_fee": "0.63"},
            decision="alerted",
        )
        rows = await repo.list_all()
        assert rows[0].net_micros == 6_253_600


class TestEvaluatePairEndToEnd:
    @staticmethod
    def equivalent_markets() -> tuple[dict[str, object], dict[str, object]]:
        kalshi: dict[str, object] = {
            "ticker": "KXBTCD-26JUN30-T70000",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "rules_primary": "Settles from the Coindesk BTC price index.",
            "resolution_source": "coindesk btc price index",
            "void_policy": "none",
        }
        poly: dict[str, object] = {
            "conditionId": "0xabc",
            "question": "Bitcoin above $70,000 on June 30?",
            "endDate": "2026-06-30T16:00:00Z",
            "description": "Settles from the Coindesk BTC price index.",
            "resolutionSource": "coindesk btc price index",
            "voidPolicy": "none",
            "clobTokenIds": '["111", "222"]',
            "active": True,
        }
        return kalshi, poly

    def test_btc_strike_market_match(self) -> None:
        kalshi_market = {
            "ticker": "KXBTCD-26JUN30-T70000",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
            "category": "crypto",
            "rules_primary": "coindesk index",
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Will BTC be above $70k on June 30?",
            "endDate": "2026-06-30T16:00:00Z",
            "resolutionSource": "coindesk index",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Crypto"],
        }
        pair = evaluate_pair(kalshi_market, poly_market)
        assert pair is not None
        assert pair.status in (MatchStatus.ACCEPTED, MatchStatus.MANUAL_REVIEW)
        assert pair.confidence >= 0.7
        assert pair.poly_yes_token_id == "111"

    def test_trap_close_end_dates_accept_with_timing_risk_flag(self) -> None:
        # Same title and threshold, end timestamps one day apart: a settlement
        # timing risk flag (same event, buffer day), not a rejection. A
        # materially larger horizon gap (>7d) is covered by
        # test_trap_material_end_date_gap_rejected.
        kalshi_market = {
            "ticker": "K1",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Bitcoin above $70,000 on June 30?",
            "endDate": "2026-07-01T16:00:00Z",  # same title, 1-day timing gap
            "clobTokenIds": '["111", "222"]',
        }
        pair = evaluate_pair(kalshi_market, poly_market)
        assert pair is not None
        assert pair.status is MatchStatus.ACCEPTED
        assert any("determination time differs" in flag for flag in pair.rule_warnings)

    def test_trap_material_end_date_gap_rejected(self) -> None:
        # Horizons more than a week apart imply a different resolution question.
        kalshi_market = {
            "ticker": "K1",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Bitcoin above $70,000 on June 30?",
            "endDate": "2026-08-30T16:00:00Z",  # two months later
            "clobTokenIds": '["111", "222"]',
        }
        pair = evaluate_pair(kalshi_market, poly_market)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("determination" in v for v in pair.differing_fields.values())

    def test_market_without_tokens_skipped(self) -> None:
        assert evaluate_pair({"ticker": "K"}, {"question": "x"}) is None

    def test_dict_shaped_gamma_tags_are_supported(self) -> None:
        pair = evaluate_pair(
            {
                "ticker": "KSPORT",
                "title": "Team A wins",
                "expected_expiration_time": "2026-06-30T16:00:00Z",
            },
            {
                "conditionId": "0xsport",
                "question": "Team A wins",
                "endDate": "2026-06-30T16:00:00Z",
                "clobTokenIds": '["yes", "no"]',
                "tags": [{"label": "Sports"}],
            },
        )
        assert pair is not None

    def test_gamma_group_item_threshold_is_not_treated_as_strike(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KTEST-26"
        kalshi["title"] = "Will Alice win the election?"
        poly["question"] = "Will Alice win the election?"
        poly["groupItemThreshold"] = "1"

        pair = evaluate_pair(kalshi, poly)

        assert pair is not None
        assert pair.matched_fields["kalshi_threshold"] is None
        assert pair.matched_fields["poly_threshold"] is None
        assert pair.status is MatchStatus.ACCEPTED

    def test_one_sided_line_market_is_not_accepted(self) -> None:
        # The live WNBA-totals false-match shape: Kalshi carries the line in a
        # structured field (floor_strike) while the Polymarket counterpart's
        # line is not parseable, so every rung of the total-points ladder would
        # otherwise match the same Polymarket market. The line IS the market's
        # identity, so a one-sided line must block acceptance, not ride along.
        kalshi = {
            "ticker": "KXWNBATOTAL-26JUN21NYLA-179",
            "title": "New York vs Los Angeles",
            "yes_sub_title": "Over 178.5 points scored",
            "floor_strike": "178.5",
            "strike_type": "greater",
            "expected_expiration_time": "2026-06-22T02:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xwnba",
            "question": "New York vs Los Angeles",
            "endDate": "2026-06-22T02:00:00Z",
            "clobTokenIds": '["111", "222"]',
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.matched_fields["kalshi_threshold"] == "178.5"
        assert pair.matched_fields["poly_threshold"] is None
        assert pair.status is not MatchStatus.ACCEPTED
        assert any("line" in reason or "threshold" in reason for reason in pair.status_reasons)

    def test_matching_line_markets_still_accept(self) -> None:
        # When both venues carry the same line the pair accepts as usual.
        kalshi = {
            "ticker": "KXWNBATOTAL-26JUN21NYLA-179",
            "title": "New York vs Los Angeles over 178.5 points",
            "floor_strike": "178.5",
            "strike_type": "greater",
            "expected_expiration_time": "2026-06-22T02:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xwnba",
            "question": "Will New York vs Los Angeles score over 178.5 points?",
            "endDate": "2026-06-22T02:00:00Z",
            "clobTokenIds": '["111", "222"]',
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.matched_fields["kalshi_threshold"] == "178.5"
        assert pair.matched_fields["poly_threshold"] == "178.5"
        assert pair.status is MatchStatus.ACCEPTED

    def test_different_election_office_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KTEST-26"
        kalshi["title"] = "Will Republicans win the Senate race in South Carolina?"
        poly["question"] = "Will Republicans win the governor race in South Carolina?"

        pair = evaluate_pair(kalshi, poly)

        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("senate_winner" in reason for reason in pair.status_reasons)

    def test_ticker_state_conflict_is_rejected_but_not_used_to_accept(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "SENATESC-26-R"
        kalshi["title"] = "Will Republicans win the Senate race?"
        poly["question"] = "Will Republicans win the Senate race in New York?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("state SC" in reason and "NY" in reason for reason in pair.status_reasons)

    def test_party_outcome_conflict_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "GOVPARTYSC-26-R"
        kalshi["title"] = "Will Republicans win the governor race in South Carolina?"
        poly["question"] = "Will Democrats win the governor race in South Carolina?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("outcome party" in reason for reason in pair.status_reasons)

    def test_primary_placement_vs_general_winner_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KXPRIMARYPLACE-GOVSCNOMR26-2-AWIL"
        kalshi["title"] = "Will Alice finish 2nd in the gubernatorial primary?"
        poly["question"] = "Will Alice win the governor election?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("primary_placement" in reason for reason in pair.status_reasons)

    def test_margin_contract_does_not_match_outright_winner(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KTEST-26"
        kalshi["title"] = "Will Alice's margin of victory be at least 5 percentage points?"
        poly["question"] = "Will Alice win the election?"

        pair = evaluate_pair(kalshi, poly)

        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("margin_spread" in reason for reason in pair.status_reasons)

    def test_governor_winner_vs_nominee_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KXGOVSCNOMR-26-AWIL"
        kalshi["title"] = "Will Alice be the Republican nominee for Governor?"
        poly["question"] = "Will Alice win the governor election?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("party_nominee" in reason for reason in pair.status_reasons)

    def test_senate_winner_vs_senate_control_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "SENATESC-26-R"
        kalshi["title"] = "Will Republicans win the Senate race in South Carolina?"
        poly["question"] = "Will Republicans control the Senate after the election?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("party_control" in reason for reason in pair.status_reasons)

    def test_sports_moneyline_vs_spread_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "KXSPORT"
        kalshi["category"] = "sports"
        kalshi["title"] = "Will Boston beat New York?"
        poly["question"] = "Will Boston cover a spread of -3.5 against New York?"
        poly["category"] = "sports"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("sports_moneyline" in reason for reason in pair.status_reasons)

    def test_crypto_threshold_vs_exact_price_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["title"] = "Will Bitcoin be above $70,000 on June 30?"
        poly["question"] = "Will Bitcoin be exactly $70,000 on June 30?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("crypto_exact_price" in reason for reason in pair.status_reasons)

    def test_same_event_and_market_type_remains_eligible_for_review(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["ticker"] = "GOVPARTYSC-26-R"
        kalshi["title"] = "Will Republicans win the governor race in South Carolina?"
        poly["question"] = "Will Republicans win the governor race in South Carolina?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is not MatchStatus.REJECTED
        assert pair.matched_fields["kalshi_market_type"] == "governor_winner"
        assert pair.matched_fields["poly_market_type"] == "governor_winner"

    def test_rule_policy_evidence_is_persisted(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi["rules_primary"] = "DNP is void and settles at fair value if cancelled."
        poly["description"] = "DNP is void after cancellation; UMA disputes may apply."
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        excerpts = pair.metadata_excerpts
        assert excerpts["kalshi"]["dnp_policy"] == "dnp"
        assert "cancelled" in excerpts["kalshi"]["sports_policy_terms"]
        assert "uma" in excerpts["polymarket"]["dispute_terms"]

    @staticmethod
    def govparty_style_markets() -> tuple[dict[str, object], dict[str, object]]:
        """Mirrors the live GOVPARTYSC-26-R pair verified 2026-06-11.

        No structured resolution_source/void_policy on either side — exactly the
        shape that previously landed in manual_review.
        """
        kalshi: dict[str, object] = {
            "ticker": "GOVPARTYSC-26-R",
            "event_ticker": "GOVPARTYSC-26",
            "title": "Will the Republican party win the governorship in South Carolina",
            "close_time": "2027-11-03T15:00:00Z",
            "expected_expiration_time": "2027-01-24T15:00:00Z",
            "can_close_early": True,
            "category": "Elections",
            "rules_primary": (
                "If a representative of the Republican party is inaugurated as the "
                "governor of South Carolina pursuant to the 2026 election, then the "
                "market resolves to Yes."
            ),
        }
        poly: dict[str, object] = {
            "conditionId": "0x1b7fa10e",
            "question": "Will the Republicans win the South Carolina governor race in 2026?",
            "clobTokenIds": '["111", "222"]',
            "active": True,
            "description": (
                "This market will resolve according to the winner of the 2026 South "
                "Carolina gubernatorial election. A candidate shall be considered to "
                "represent a party in the event that he or she is the nominee of the "
                "party in question. The resolution source for this market is the "
                "Associated Press, Fox News, and NBC. This market will resolve once "
                "all three sources call the race for the same candidate."
            ),
        }
        return kalshi, poly

    def test_govparty_settlement_basis_conflict_is_rejected_not_manual_review(self) -> None:
        kalshi, poly = self.govparty_style_markets()
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        # REJECTED, therefore not MANUAL_REVIEW: the pair must not stay reviewable.
        assert pair.status is MatchStatus.REJECTED
        assert any("settlement_basis_conflict" in reason for reason in pair.status_reasons)
        assert any("settlement_basis_conflict" in str(v) for v in pair.differing_fields.values())

    def test_same_winner_basis_on_both_venues_is_not_basis_rejected(self) -> None:
        # Guard: the new rule must not reject pairs where both venues use the
        # election-winner basis; those stay with the existing conservative checks.
        kalshi, poly = self.govparty_style_markets()
        kalshi["rules_primary"] = (
            "Resolves according to the winner of the 2026 South Carolina "
            "gubernatorial election."
        )
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert not any("settlement_basis_conflict" in reason for reason in pair.status_reasons)
        assert pair.status is not MatchStatus.ACCEPTED  # still missing rule facts

    def test_crypto_threshold_vs_best_month_is_rejected_end_to_end(self) -> None:
        # The distinguishing language lives in the titles, so this proves the
        # titles reach the rule-equivalence detectors through discovery.
        kalshi = {
            "ticker": "KXBTCMAX100-26-SEP",
            "title": "Will Bitcoin be above $100000 by October 1, 2026 at 12:00AM ET?",
            "expected_expiration_time": "2026-10-01T04:00:00Z",
            "rules_primary": (
                "If the price of Bitcoin is above $100,000 before October 1, 2026 "
                "at 12:00 AM ET, then the market resolves to Yes."
            ),
        }
        poly = {
            "conditionId": "0xbestmonth",
            "question": "Will October be the best month for Bitcoin in 2026?",
            "clobTokenIds": '["111", "222"]',
            "description": (
                "This market will resolve to the calendar month during which "
                "Bitcoin has the highest percentage change in 2026."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any(
            "crypto_performance_vs_price_threshold_conflict" in reason
            for reason in pair.status_reasons
        )

    def test_knockout_stage_count_vs_world_cup_winner_is_rejected_end_to_end(self) -> None:
        kalshi = {
            "ticker": "KXWCREGIONKO-26SA-2",
            "title": (
                "Will at least 2 teams from South America reach the knockout "
                "stage of the 2026 Men's FIFA World Cup?"
            ),
            "expected_expiration_time": "2026-07-19T22:00:00Z",
            "rules_primary": (
                "If at least 2 teams from South America reach the knockout stage "
                "of the 2026 Men's FIFA World Cup, then the market resolves to Yes."
            ),
        }
        poly = {
            "conditionId": "0xsawins",
            "question": "Will South America win the 2026 FIFA World Cup?",
            "clobTokenIds": '["111", "222"]',
            "description": (
                "This market will resolve to the continent of the country that "
                "wins the 2026 FIFA World Cup."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any(
            "sports_stage_vs_winner_conflict" in reason for reason in pair.status_reasons
        )

    def test_wc_continent_pair_surfaces_void_policy_mismatch(self) -> None:
        # The verified KXWCCONTINENT-26-SA pair: normal-state outcomes
        # coincide, but Polymarket proves resolves-to-Other cancellation
        # while Kalshi's fair-value handling is invisible to the scanner.
        # Must stay manual_review with an explicit mismatch reason.
        kalshi = {
            "ticker": "KXWCCONTINENT-26-SA",
            "title": "Will South America (CONMEBOL) win the 2026 Men's World Cup?",
            "expected_expiration_time": "2026-07-20T00:00:00Z",
            "rules_primary": (
                "If any country that competes in South America (CONMEBOL) "
                "qualification is the 2026 FIFA Men's World Cup champion, then "
                "the market resolves to Yes."
            ),
        }
        poly = {
            "conditionId": "0x0ed2e5e9",
            "question": "Will South America (CONMEBOL) win the 2026 FIFA World Cup?",
            "clobTokenIds": '["111", "222"]',
            "description": (
                "This market will resolve to the continent of the country that "
                "wins the 2026 FIFA World Cup. If the 2026 FIFA World Cup is "
                "cancelled, postponed after December 31, 2026, or there is "
                "otherwise no winner declared within that timeframe, this market "
                "will resolve to “Other”."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        # Same event; the cancellation-tail divergence is surfaced as a risk
        # flag for the human to verify rather than blocking the opportunity.
        assert pair.status is MatchStatus.ACCEPTED
        assert any("void_policy_mismatch" in flag for flag in pair.rule_warnings)
        assert "void_policy_basis" in pair.missing_rule_fields
        excerpts = pair.metadata_excerpts
        assert excerpts["polymarket"]["cancellation_policy_basis"] == "resolves_to_other"
        assert excerpts["kalshi"]["cancellation_policy_basis"] is None

    def test_proven_incompatible_cancellation_policies_are_risk_flag(self) -> None:
        # Even a proven fair-value vs resolves-to-other cancellation basis is
        # identical in normal resolution; the divergence is a cancellation-tail
        # risk flag (operator model §18), not a different-event rejection.
        kalshi, poly = self.equivalent_markets()
        kalshi["rules_primary"] = (
            str(kalshi["rules_primary"]) + " If the event is cancelled outright, "
            "Yes holders receive the last traded price prior to cancellation."
        )
        poly["description"] = (
            str(poly["description"]) + " If the event is cancelled, this market "
            "will resolve to “Other”."
        )
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.ACCEPTED
        assert any("void_policy_conflict" in flag for flag in pair.rule_warnings)

    def test_spx_snapshot_vs_official_close_surfaces_finalization_mismatch(self) -> None:
        # The verified KXINXDIRY pair: boundary and date match, but the
        # underlying is finalized differently. Stays manual_review with the
        # explicit diagnostic.
        kalshi = {
            "ticker": "KXINXDIRY-26DEC31H1600-T8000",
            "title": "Will the S&P 500 be above 8000 on Dec 31, 2026 at 4pm EST?",
            "expected_expiration_time": "2026-12-31T21:00:00Z",
            "rules_primary": (
                "If the S&P 500 index value on Dec 31, 2026 at 4pm EST is above "
                "8000, then the market resolves to Yes."
            ),
        }
        poly = {
            "conditionId": "0x8b13efb0",
            "question": "Will S&P 500 (SPX) close over $8,000 on the final trading "
            "day of December 2026?",
            "clobTokenIds": '["111", "222"]',
            "description": (
                "This market will resolve to Yes if the official closing price "
                "for S&P 500 (SPX) on the final trading day of December 2026 is "
                "higher than the listed price. The resolution source for this "
                "market is Yahoo Finance, under Historical Prices."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.MANUAL_REVIEW
        assert any("source_finalization_mismatch" in reason for reason in pair.status_reasons)
        assert "source_finalization_basis" in pair.missing_rule_fields
        excerpts = pair.metadata_excerpts
        assert excerpts["kalshi"]["source_finalization_basis"] == "fixed_time_snapshot"
        assert excerpts["polymarket"]["source_finalization_basis"] == "official_close"

    def test_progressive_slate_vs_incumbent_cohort_is_rejected_end_to_end(self) -> None:
        # The verified KXDEMPROGRESSIVESENATESWEEP pair: fixed named slate vs
        # registration-dependent incumbent cohort. Provably different sets.
        kalshi = {
            "ticker": "KXDEMPROGRESSIVESENATESWEEP-26NOV03",
            "title": (
                "Will the listed Democratic Senate candidates all win their "
                "primary elections?"
            ),
            "expected_expiration_time": "2026-11-03T15:00:00Z",
            "rules_primary": (
                "If ALL of the following Democratic candidates win their 2026 "
                "Senate primary elections: Juliana Stratton in Illinois, Graham "
                "Platner in Maine, Mallory McMorrow OR Abdul El-Sayed in "
                "Michigan, Peggy Flanagan in Minnesota, and Ed Markey in "
                "Massachusetts, then the market resolves to Yes."
            ),
        }
        poly = {
            "conditionId": "0x21ac6c0f",
            "question": (
                "Will Democratic Senate incumbents win all their nominating "
                "elections in the 2026 cycle?"
            ),
            "clobTokenIds": '["111", "222"]',
            "description": (
                "This market will resolve according to the number of Democratic "
                "Senate incumbents who do not win their nominating election to "
                "move on to the general election as a result of the 2026 midterm "
                "primary elections. Incumbents who do not officially register as "
                "candidates for reelection will not be considered."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("candidate_set_conflict" in reason for reason in pair.status_reasons)

    def test_player_award_vs_trade_is_rejected_end_to_end(self) -> None:
        # Live 10k-run family: same athlete, unrelated propositions.
        kalshi = {
            "ticker": "KXNFLDPOTY-27-KTHI",
            "title": "Will Kayvon Thibodeaux win the Defensive Player of the Year?",
            "expected_expiration_time": "2027-02-15T00:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xtrade",
            "question": "Will Kayvon Thibodeaux be traded?",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Sports"],
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("player_prop_scope_conflict" in r for r in pair.status_reasons)

    def test_same_stat_leader_pair_is_not_prop_rejected(self) -> None:
        # Same statistic, different phrasings: must NOT be rejected by the
        # prop rule (stays with the ordinary conservative checks).
        kalshi = {
            "ticker": "KXLEADERMLBKS-26-YYAM",
            "title": (
                "Will Yoshinobu Yamamoto lead Pro Baseball in strikeouts for "
                "the 2026 regular season?"
            ),
            "expected_expiration_time": "2026-10-01T00:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xkleader",
            "question": (
                "Will Yoshinobu Yamamoto strike out the most batters during "
                "the 2026 MLB regular season?"
            ),
            "clobTokenIds": '["111", "222"]',
            "tags": ["Sports"],
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert not any("player_prop_scope_conflict" in r for r in pair.status_reasons)
        assert pair.status is not MatchStatus.ACCEPTED  # rule facts still unverified

    @staticmethod
    def dc_mayor_markets(
        kalshi_candidate: str, poly_candidate: str
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Live KXDCMAYORD shape (docs/VERIFICATION.md §15): generic Kalshi
        categorical title, candidate in custom_strike/yes_sub_title."""
        kalshi: dict[str, object] = {
            "ticker": "KXDCMAYORD-26-X",
            "title": "Who will win the 2026 D.C. Democratic Mayoral Primary?",
            "yes_sub_title": kalshi_candidate,
            "custom_strike": {"Candidate/Party": kalshi_candidate},
            "expected_expiration_time": "2026-06-16T15:00:00Z",
            "rules_primary": (
                f"If {kalshi_candidate} wins the 2026 D.C. Democratic Mayoral "
                "Primary in 2026, then the market resolves to Yes."
            ),
        }
        poly: dict[str, object] = {
            "conditionId": "0xdcmayor",
            "question": (
                f"Will {poly_candidate} win the 2026 Democratic D.C. Mayoral Primary?"
            ),
            "clobTokenIds": '["111", "222"]',
            "endDate": "2026-06-16T15:00:00Z",
        }
        return kalshi, poly

    def test_dc_mayor_different_candidates_rejected_with_entity_conflict(self) -> None:
        kalshi, poly = self.dc_mayor_markets("Brianne K. Nadeau", "Christina Henderson")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("outcome entity" in reason for reason in pair.status_reasons)
        assert pair.matched_fields["kalshi_outcome_entity"] == "brianne k nadeau"
        assert pair.matched_fields["poly_outcome_entity"] == "christina henderson"

    def test_dc_mayor_same_candidate_accepts_with_risk_flags(self) -> None:
        kalshi, poly = self.dc_mayor_markets("Brianne K. Nadeau", "Brianne K. Nadeau")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert not any("outcome entity" in reason for reason in pair.status_reasons)
        # Same candidate, no different-event conflict: accepts with the
        # unverified settlement facts carried as risk flags.
        assert pair.status is MatchStatus.ACCEPTED
        assert pair.rule_warnings

    def test_dc_mayor_subset_name_not_rejected(self) -> None:
        # Missing middle initial is the same person, never a conflict.
        kalshi, poly = self.dc_mayor_markets("Brianne K. Nadeau", "Brianne Nadeau")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert not any("outcome entity" in reason for reason in pair.status_reasons)

    def test_dc_mayor_entity_from_custom_strike_when_subtitle_generic(self) -> None:
        kalshi, poly = self.dc_mayor_markets("Kenyan McDuffie", "Kenyan McDuffie")
        kalshi["yes_sub_title"] = "Yes"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        evidence = pair.matched_fields["kalshi_outcome_entity_evidence"]
        assert evidence is not None and evidence["source"] == "custom_strike"

    def test_dc_mayor_one_sided_entity_is_unverified_manual_review(self) -> None:
        kalshi, poly = self.dc_mayor_markets("Brianne K. Nadeau", "Brianne K. Nadeau")
        # Polymarket title without the "Will <name> win" shape: no extraction.
        poly["question"] = "2026 Democratic D.C. Mayoral Primary winner market"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is not MatchStatus.ACCEPTED
        if pair.status is MatchStatus.MANUAL_REVIEW:
            assert any("outcome_entity unverified" in r for r in pair.status_reasons)
            assert "outcome_entity" in pair.missing_rule_fields

    def test_mlb_stat_leader_pair_surfaces_tie_policy_mismatch(self) -> None:
        # The verified Skubal pair (docs/VERIFICATION.md §17): same player,
        # stat, and season scope, but ties pay proportionally on Kalshi and a
        # single tiebroken winner on Polymarket. Stays manual_review with the
        # explicit diagnostic; never accepted.
        kalshi = {
            "ticker": "KXLEADERMLBKS-26-TSKUBAL29",
            "title": "Will Tarik Skubal lead Pro Baseball in strikeouts for "
            "the 2026 regular season?",
            "yes_sub_title": "Tarik Skubal",
            "expected_expiration_time": "2026-10-15T14:00:00Z",
            "category": "sports",
            "rules_primary": (
                "If Tarik Skubal leads Pro Baseball in strikeouts for the 2026 "
                "regular season, then the market resolves to Yes. In case of "
                "exact ties where the league does not declare a single winner, "
                "tied participants receive a proportional payout."
            ),
        }
        poly = {
            "conditionId": "0x9687fa18",
            "question": "Will Tarik Skubal strike out the most batters during "
            "the 2026 MLB regular season?",
            "groupItemTitle": "Tarik Skubal",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Sports"],
            "description": (
                "This market will resolve according to the pitcher who records "
                "the most strikeouts during the 2026 MLB regular season. In the "
                "event of a tie, if a tie still persists, this market will "
                "resolve to the pitcher whose listed last name comes first "
                "alphabetically."
            ),
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.MANUAL_REVIEW
        assert any("stat_leader_rule_mismatch" in r for r in pair.status_reasons)
        assert "stat_leader_tie_policy" in pair.missing_rule_fields
        assert pair.matched_fields["kalshi_outcome_entity"] == "tarik skubal"
        assert pair.matched_fields["poly_outcome_entity"] == "tarik skubal"
        excerpts = pair.metadata_excerpts
        assert excerpts["kalshi"]["stat_tie_policy"] == "ties_split"
        assert excerpts["polymarket"]["stat_tie_policy"] == "sole_winner_tiebreak"

    def test_accented_name_pair_extracts_matching_entities(self) -> None:
        kalshi = {
            "ticker": "KXLEADERMLBKS-26-CSANCHEZ61",
            "title": "Will Cristopher Sánchez lead Pro Baseball in strikeouts "
            "for the 2026 regular season?",
            "yes_sub_title": "Cristopher Sánchez",
            "expected_expiration_time": "2026-10-15T14:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xsanchez",
            "question": "Will Cristopher Sánchez strike out the most batters "
            "during the 2026 MLB regular season?",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Sports"],
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.matched_fields["kalshi_outcome_entity"] == "cristopher sanchez"
        assert pair.matched_fields["poly_outcome_entity"] == "cristopher sanchez"
        assert not any("outcome entity" in r for r in pair.status_reasons)

    def test_all_star_vs_strikeout_leader_is_rejected_end_to_end(self) -> None:
        kalshi = {
            "ticker": "KXMLBALLSTAR-26NL-YYAMAMOTO18",
            "title": "Will Yoshinobu Yamamoto be selected to the 2026 NL All-Star Team?",
            "expected_expiration_time": "2026-07-10T00:00:00Z",
            "category": "sports",
        }
        poly = {
            "conditionId": "0xyamamoto",
            "question": "Will Yoshinobu Yamamoto strike out the most batters "
            "during the 2026 MLB regular season?",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Sports"],
        }
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("player_prop_scope_conflict" in r for r in pair.status_reasons)

    def test_title_match_but_missing_rules_accepts_with_risk_flags(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi.pop("rules_primary")
        kalshi.pop("resolution_source")
        poly.pop("description")
        poly.pop("resolutionSource")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        # Unverifiable rules text/source are risk flags, not a manual_review
        # block; the missing fields are still recorded for the human.
        assert pair.status is MatchStatus.ACCEPTED
        assert "resolution_source" in pair.missing_rule_fields
        assert "resolution_text" in pair.missing_rule_fields

    def test_title_match_but_different_event_date_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        poly["question"] = "Bitcoin above $70,000 on July 1?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("event_date" in reason for reason in pair.status_reasons)

    def test_title_match_but_different_threshold_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        poly["question"] = "Bitcoin above $80,000 on June 30?"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("strike" in reason or "threshold" in reason for reason in pair.status_reasons)

    def test_different_resolution_source_is_risk_flag(self) -> None:
        kalshi, poly = self.equivalent_markets()
        poly["resolutionSource"] = "coinbase btc price"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        # Different source wording is a verify-this risk flag, not proof of a
        # different event.
        assert pair.status is MatchStatus.ACCEPTED
        assert any("resolution source" in flag for flag in pair.rule_warnings)

    def test_missing_void_policy_accepts_with_risk_flag(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi.pop("void_policy")
        poly.pop("voidPolicy")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.ACCEPTED
        assert "void_policy" in pair.missing_rule_fields

    def test_equivalent_structured_metadata_is_accepted(self) -> None:
        kalshi, poly = self.equivalent_markets()
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.ACCEPTED
