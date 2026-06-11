"""Storage round-trip tests against in-memory SQLite."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arb_scanner.app.markets.discovery import MatchedPair, evaluate_pair
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.repo import (
    OpportunityRepo,
    PairRepo,
    SnapshotRepo,
    SqlAlchemyScanStore,
)
from arb_scanner.app.storage.reporting import render_pair_row


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

    def test_trap_different_end_dates_rejected(self) -> None:
        kalshi_market = {
            "ticker": "K1",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Bitcoin above $70,000 on June 30?",
            "endDate": "2026-07-01T16:00:00Z",  # same title, different determination
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

    def test_title_match_but_missing_rules_is_manual_review(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi.pop("rules_primary")
        kalshi.pop("resolution_source")
        poly.pop("description")
        poly.pop("resolutionSource")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.MANUAL_REVIEW
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

    def test_different_resolution_source_is_rejected(self) -> None:
        kalshi, poly = self.equivalent_markets()
        poly["resolutionSource"] = "coinbase btc price"
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("resolution source" in reason for reason in pair.status_reasons)

    def test_missing_void_policy_is_manual_review(self) -> None:
        kalshi, poly = self.equivalent_markets()
        kalshi.pop("void_policy")
        poly.pop("voidPolicy")
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.MANUAL_REVIEW
        assert "void_policy" in pair.missing_rule_fields

    def test_equivalent_structured_metadata_is_accepted(self) -> None:
        kalshi, poly = self.equivalent_markets()
        pair = evaluate_pair(kalshi, poly)
        assert pair is not None
        assert pair.status is MatchStatus.ACCEPTED
