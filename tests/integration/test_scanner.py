"""End-to-end discovery scan tests over deterministic venue stubs."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from arb_scanner.app.alerts.base import AlertPayload
from arb_scanner.app.clients.polymarket_gamma import GammaDiscoveryResult
from arb_scanner.app.economics import CostAssumptions, Direction
from arb_scanner.app.markets.polymarket import PolymarketMarket
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.risk.controls import RiskLimits
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.scanner import (
    _candidate_positions,
    _poly_candidate_index,
    _primary_rejection_bucket,
    hold_days_from_markets,
    quote_age_seconds,
    scan_once,
)
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.repo import (
    OpportunityRepo,
    PairRepo,
    SnapshotRepo,
    SqlAlchemyScanStore,
)
from arb_scanner.app.types import Money, OrderBook, Side, Venue

NOW = datetime(2026, 6, 11, 11, 43, 26, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def test_candidate_prefilter_recall_survives_corpus_growth() -> None:
    """Pairs found in a small window must still be found in a larger one.

    Regression for the 10k-market recall collapse (2026-06-11): absolute
    token-breadth caps silently dropped known pairs as the corpus grew.
    """
    from arb_scanner.app.markets.polymarket import PolymarketMarket
    from arb_scanner.app.scanner import _poly_candidate_index

    kalshi_market = {"title": "Will South America (CONMEBOL) win the 2026 Men's World Cup?"}
    target = PolymarketMarket.from_gamma(
        {
            "conditionId": "0xtarget",
            "question": "Will South America (CONMEBOL) win the 2026 FIFA World Cup?",
            "clobTokenIds": '["1", "2"]',
            "active": True,
        }
    )
    # Filler markets sharing the broad tokens (2026, world, cup) at scale.
    fillers = [
        PolymarketMarket.from_gamma(
            {
                "conditionId": f"0xf{i}",
                "question": f"Will team {i} win a 2026 world cup qualifier match {i}?",
                "clobTokenIds": '["1", "2"]',
                "active": True,
            }
        )
        for i in range(3000)
    ]
    for markets in ([target, *fillers[:500]], [target, *fillers]):
        index = _poly_candidate_index(markets)
        positions = _candidate_positions(kalshi_market, index, len(markets))
        assert 0 in positions, f"target pair lost at corpus size {len(markets)}"


def test_rejection_histogram_uses_one_primary_reason() -> None:
    assert (
        _primary_rejection_bucket(("threshold 1 != 2", "similarity below review threshold"))
        == "threshold_conflict"
    )


def test_market_type_reasons_mentioning_threshold_bucket_as_market_type() -> None:
    reason = "market type crypto_price_threshold (title) != crypto_monthly_performance (title)"
    assert _primary_rejection_bucket((reason,)) == "market_type_conflict"
    # Genuine threshold reasons keep their own bucket.
    assert _primary_rejection_bucket(("threshold 70000 (title) != 80000 (title)",)) == (
        "threshold_conflict"
    )


def test_void_policy_conflict_reason_buckets_by_name() -> None:
    reason = (
        "void_policy_conflict: incompatible cancellation settlement "
        "(kalshi=fair_value_settlement, polymarket=resolves_to_other)"
    )
    assert _primary_rejection_bucket((reason,)) == "void_policy_conflict"


def test_settlement_basis_conflict_has_its_own_histogram_bucket() -> None:
    reason = (
        "settlement_basis_conflict: Kalshi resolves on the officeholder sworn "
        "in/inaugurated while Polymarket resolves on the called/certified election winner"
    )
    assert _primary_rejection_bucket((reason,)) == "settlement_basis_conflict"
    # Named conflicts outrank the catch-all and the similarity bucket.
    assert (
        _primary_rejection_bucket((reason, "similarity below review threshold"))
        == "settlement_basis_conflict"
    )


def test_new_named_conflicts_have_their_own_histogram_buckets() -> None:
    for reason_prefix in (
        "continent_scope_conflict",
        "sports_stage_vs_winner_conflict",
        "crypto_performance_vs_price_threshold_conflict",
        "stock_close_vs_intramonth_high_conflict",
    ):
        reason = f"{reason_prefix}: one venue resolves differently from the other"
        assert _primary_rejection_bucket((reason,)) == reason_prefix
        # Named text-evidence conflicts outrank the generic market-type bucket.
        assert (
            _primary_rejection_bucket(
                (
                    reason,
                    "market type crypto_price_threshold (title) != "
                    "crypto_monthly_performance (title)",
                )
            )
            == reason_prefix
        )


def test_office_level_and_basket_scope_have_their_own_histogram_buckets() -> None:
    office = (
        "office_level_conflict: one venue resolves on state legislative chamber "
        "control and the other on the U.S. Senate race"
    )
    basket = (
        "basket_scope_conflict: kalshi requires multiple states to all resolve "
        "the same way while polymarket covers a single race"
    )
    assert _primary_rejection_bucket((office,)) == "office_level_conflict"
    assert _primary_rejection_bucket((basket,)) == "basket_scope_conflict"
    assert (
        _primary_rejection_bucket((basket, "similarity below review threshold"))
        == "basket_scope_conflict"
    )


KALSHI_MARKET = {
    "ticker": "KXBTCD-26JUN30-T70000",
    "title": "Bitcoin above $70,000 on June 30?",
    "expected_expiration_time": "2026-06-30T16:00:00Z",
    "can_close_early": False,
    "status": "active",
    "rules_primary": "Settles from the Coindesk BTC price index at 16:00 UTC.",
    "resolution_source": "coindesk btc price index",
    "void_policy": "none",
}
POLY_MARKET = {
    "conditionId": "0xabc",
    "question": "Will BTC be above $70k on June 30?",
    "description": "Settles from the Coindesk BTC price index at 16:00 UTC.",
    "endDate": "2026-06-30T16:00:00Z",
    "resolutionSource": "coindesk btc price index",
    "voidPolicy": "none",
    "clobTokenIds": '["111", "222"]',
    "tags": ["Crypto"],
    "feeSchedule": {"rate": "0.05", "exponent": "1", "takerOnly": True},
    "outcomes": '["Yes", "No"]',
    "active": True,
    "closed": False,
    "archived": False,
    "acceptingOrders": True,
    "enableOrderBook": True,
}

# Kalshi YES asks come from NO bids: NO bid 0.10 -> YES ask 0.90.
KALSHI_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.0500", "500.00"]],
        "no_dollars": [["0.1000", "500.00"]],
    }
}
POLY_NO_BOOK = {
    "asset_id": "222",
    "timestamp": str(NOW_MS),
    "bids": [],
    "asks": [{"price": "0.03", "size": "500"}],
}
POLY_YES_BOOK = {
    "asset_id": "111",
    "timestamp": str(NOW_MS),
    "bids": [],
    "asks": [{"price": "0.99", "size": "500"}],
}


def known_costs() -> CostAssumptions:
    zero = Money.zero()
    return CostAssumptions(
        bridge_cost=zero,
        withdrawal_cost=zero,
        gas_cost=zero,
        processor_cost=zero,
        conversion_cost=zero,
    )


class StubKalshi:
    def __init__(self, book: dict[str, Any] | None = None) -> None:
        self.book = book or KALSHI_BOOK
        self.discovery_kwargs: dict[str, Any] = {}

    async def get_all_markets(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.discovery_kwargs = kwargs
        return [KALSHI_MARKET]

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        return self.book


class StubGamma:
    def __init__(
        self,
        market: dict[str, Any] | None = None,
        *,
        markets: list[dict[str, Any]] | None = None,
    ) -> None:
        self.markets = markets or [market or POLY_MARKET]

    async def get_all_markets(self, **kwargs: Any) -> GammaDiscoveryResult:
        return GammaDiscoveryResult(
            markets=self.markets,
            pages_fetched=1,
            total_fetched=len(self.markets),
        )


class StubClob:
    def __init__(self, market_info: dict[str, Any] | None = None) -> None:
        self.market_info = market_info or {}

    async def get_book(self, token_id: str) -> dict[str, Any]:
        return POLY_NO_BOOK if token_id == "222" else POLY_YES_BOOK

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        return self.market_info


class CapturingSink:
    def __init__(self) -> None:
        self.sent: list[AlertPayload] = []

    async def send(self, payload: AlertPayload) -> None:
        self.sent.append(payload)


def permissive_limits(**overrides: Any) -> RiskLimits:
    values: dict[str, Any] = {
        "min_net_profit": Money.from_dollars("1"),
        "min_match_confidence": 0.8,
    }
    values.update(overrides)
    return RiskLimits(**values)


class TestScanOnce:
    async def test_full_trail_produces_alert_and_uses_mve_filter(self) -> None:
        sink = CapturingSink()
        kalshi = StubKalshi()
        report = await scan_once(
            kalshi=kalshi,
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[sink],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        assert kalshi.discovery_kwargs["mve_filter"] == "exclude"
        assert report.kalshi_markets_discovered == 1
        assert report.kalshi_markets_scannable == 1
        assert report.poly_markets_discovered == 1
        assert report.poly_markets_scannable == 1
        assert report.raw_title_candidates == 1
        assert report.structured_candidates == 1
        assert report.manual_review_candidates == 0
        assert report.pairs_accepted == 1
        alerted = [opportunity for opportunity in report.opportunities if not opportunity[2]]
        assert alerted
        assert sink.sent and sink.sent[0].net_edge > Money.zero()
        assert any("ALERT" in line for line in report.render_lines())
        # The emitted alert carries the settlement caveats to verify (at least
        # the ever-present UMA challenge window on a Polymarket-resolved pair).
        assert any("UMA challenge window" in flag for flag in sink.sent[0].risk_flags)
        # The fully-matching fixture (same Coindesk source, same determination)
        # auto-verifies: UMA is acknowledged, nothing left for a human.
        assert sink.sent[0].verification_verdict == "verified"
        assert sink.sent[0].unresolved_flags == ()

    async def test_verifier_suppresses_proven_source_divergence(self) -> None:
        # Kalshi settles from Coindesk, this Polymarket market from Binance —
        # different price sources can disagree near the strike, so the verifier
        # rejects the pair and the alert is auto-suppressed (recorded, not sent).
        market = {
            **POLY_MARKET,
            "description": "Settles from the Binance BTC price index.",
            "resolutionSource": "binance btc price",
        }
        sink = CapturingSink()
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(market),
            clob=StubClob(),
            sinks=[sink],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        assert sink.sent == []
        assert any(
            "verifier" in reason.lower()
            for _, _, reasons in report.opportunities
            for reason in reasons
        )
        assert report.verification_verdicts["rejected"] >= 1

    async def test_missing_orderbook_skips_pair_without_aborting_scan(self) -> None:
        # A Polymarket token with no orderbook returns 404 from the CLOB. The
        # scan must skip that one pair and finish, not crash the whole pass.
        from arb_scanner.app.clients.base import NotFoundError

        class NoBookClob(StubClob):
            async def get_book(self, token_id: str) -> dict[str, Any]:
                raise NotFoundError(
                    "404 https://clob.polymarket.com/book: "
                    '{"error":"No orderbook exists for the requested token id"}'
                )

        sink = CapturingSink()
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=NoBookClob(),
            sinks=[sink],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        # Pair was accepted by matching but produced no evaluable opportunity
        # and no alert, and the scan returned a report rather than raising.
        assert report.pairs_accepted == 1
        assert report.book_fetch_failures == 1
        assert sink.sent == []

    async def test_market_fee_metadata_is_used_in_economics(self) -> None:
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        evaluation = next(
            item[1]
            for item in report.opportunities
            if item[1].direction is Direction.KALSHI_YES_POLY_NO
        )
        assert evaluation.fees.polymarket_fee == Money.from_dollars("0.1455")

    async def test_unknown_fee_metadata_rejects(self) -> None:
        market = {key: value for key, value in POLY_MARKET.items() if key != "feeSchedule"}
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(market),
            clob=StubClob(),
            sinks=[],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        assert any(
            "Polymarket fee metadata" in reason
            for _, _, reasons in report.opportunities
            for reason in reasons
        )

    async def test_unknown_required_costs_reject(self) -> None:
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[],
            limits=permissive_limits(),
            now=NOW,
        )
        assert any(
            "unknown required costs" in reason
            for _, _, reasons in report.opportunities
            for reason in reasons
        )

    async def test_partial_fill_uses_requested_size(self) -> None:
        partial_book = {
            "orderbook_fp": {
                "yes_dollars": [["0.0500", "500.00"]],
                "no_dollars": [["0.1000", "40.00"]],
            }
        }
        report = await scan_once(
            kalshi=StubKalshi(partial_book),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[],
            limits=permissive_limits(min_fill_fraction=Decimal("0.8")),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        pair, evaluation, reasons = next(
            item
            for item in report.opportunities
            if item[1].direction is Direction.KALSHI_YES_POLY_NO
        )
        assert pair.kalshi_ticker == KALSHI_MARKET["ticker"]
        assert evaluation.requested_size == 100
        assert evaluation.executable_size == 40
        assert evaluation.fill_fraction == Decimal("0.4")
        assert any("fill fraction 0.4 < min 0.8" in reason for reason in reasons)

    async def test_file_kill_switch_blocks_alert(self, tmp_path: Path) -> None:
        flag = tmp_path / "stop"
        flag.touch()
        sink = CapturingSink()
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[sink],
            limits=permissive_limits(),
            kill_switch=KillSwitch(flag_file=flag),
            cost_assumptions=known_costs(),
            now=NOW,
        )
        assert sink.sent == []
        assert any("kill switch engaged" in reasons for _, _, reasons in report.opportunities)

    async def test_scan_persists_snapshots_and_both_decisions(self) -> None:
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await init_models(engine)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                await scan_once(
                    kalshi=StubKalshi(),
                    gamma=StubGamma(),
                    clob=StubClob(),
                    sinks=[],
                    limits=permissive_limits(),
                    cost_assumptions=known_costs(),
                    store=SqlAlchemyScanStore(session),
                    now=NOW,
                )
                await session.commit()
                opportunities = await OpportunityRepo(session).list_all()
                snapshots = await SnapshotRepo(session).list_all()
        finally:
            await engine.dispose()

        assert {row.decision for row in opportunities} == {"alerted", "rejected"}
        assert all(row.book_snapshot["format"] == "paired_opportunity" for row in opportunities)
        assert len(snapshots) == 4

    async def test_scan_persists_manual_review_and_rejection_diagnostics(self) -> None:
        # manual_review is now triggered by mid-band similarity (0.6–0.9): a
        # related-but-not-identical question. voidPolicy is also dropped so the
        # row still carries a missing settlement fact for the human.
        manual = {key: value for key, value in POLY_MARKET.items() if key != "voidPolicy"}
        manual["question"] = "Will Bitcoin exceed $70,000 sometime in late June?"
        rejected = {
            **POLY_MARKET,
            "conditionId": "0xrejected",
            "question": "BTC above $80k on June 30?",
            "clobTokenIds": '["333", "444"]',
        }
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await init_models(engine)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                await scan_once(
                    kalshi=StubKalshi(),
                    gamma=StubGamma(markets=[manual, rejected]),
                    clob=StubClob(),
                    sinks=[],
                    cost_assumptions=known_costs(),
                    store=SqlAlchemyScanStore(session),
                    now=NOW,
                )
                await session.commit()
                manual_rows = await PairRepo(session).list_by_status("manual_review")
                rejected_rows = await PairRepo(session).list_by_status("rejected")
        finally:
            await engine.dispose()

        assert len(manual_rows) == 1
        assert "void_policy" in manual_rows[0].matched_fields["missing_rule_fields"]
        assert manual_rows[0].matched_fields["metadata_excerpts"]["polymarket"]["question"]
        assert len(rejected_rows) == 1
        assert any(
            "strike" in reason or "threshold" in reason
            for reason in rejected_rows[0].matched_fields["status_reasons"]
        )


def test_hold_days_come_from_market_timestamps() -> None:
    hold = hold_days_from_markets(KALSHI_MARKET, POLY_MARKET, NOW)
    expected = Decimal(str((datetime(2026, 6, 30, 16, tzinfo=UTC) - NOW).total_seconds()))
    assert hold == expected / Decimal(86_400)


def test_quote_age_uses_oldest_snapshot_timestamp() -> None:
    fresh = OrderBook(Venue.KALSHI, "K", Side.YES, (), (), timestamp_ms=NOW_MS)
    old = OrderBook(Venue.POLYMARKET, "P", Side.NO, (), (), timestamp_ms=NOW_MS - 12_000)
    assert quote_age_seconds((fresh, old), NOW) == 12.0


def test_candidate_prefilter_uses_rare_shared_tokens() -> None:
    markets = [
        PolymarketMarket.from_gamma(
            {
                **POLY_MARKET,
                "conditionId": f"condition-{index}",
                "question": f"Will common event unique{index} code{index} happen?",
            }
        )
        for index in range(100)
    ]
    index = _poly_candidate_index(markets)
    assert _candidate_positions(
        {"title": "Will common event unique42 code42 happen?"}, index, 100
    ) == {42}
    assert _candidate_positions({"title": "Will common event happen?"}, index, 100) == set()


async def test_manual_review_is_counted_rendered_and_not_evaluated() -> None:
    market = {key: value for key, value in POLY_MARKET.items() if key != "voidPolicy"}
    # Mid-band similarity (0.6–0.9) is the manual_review trigger under the
    # same-event + risk-flags model; missing voidPolicy keeps a flag in view.
    market["question"] = "Will Bitcoin exceed $70,000 sometime in late June?"
    report = await scan_once(
        kalshi=StubKalshi(),
        gamma=StubGamma(market),
        clob=StubClob(),
        sinks=[],
        limits=permissive_limits(),
        cost_assumptions=known_costs(),
        now=NOW,
    )
    assert report.manual_review_candidates == 1
    assert report.pairs_accepted == 0
    assert report.opportunities == []
    rendered = "\n".join(report.render_manual_review_lines(1))
    assert "NOT TRADE SAFE" in rendered
    assert "void_policy" in rendered
    assert "hypothetical edge: NOT COMPUTED" in rendered


@pytest.mark.live
async def test_live_pipeline_finds_candidates_and_economics_are_bounded() -> None:
    """End-to-end proof on real venue data (run with: uv run pytest -m live).

    Unit tests cannot prove the pipeline fires on real markets. This bounded
    live pass asserts structural invariants that must hold against the actual
    Kalshi + Polymarket universe regardless of day-to-day market state:
    discovery and structured matching produce candidates, and every evaluated
    opportunity is plausibility-bounded and carries its settlement risk flags.
    """
    import aiohttp

    from arb_scanner.app.clients.kalshi_rest import KalshiRestClient
    from arb_scanner.app.clients.polymarket_clob import PolymarketClobClient
    from arb_scanner.app.clients.polymarket_gamma import PolymarketGammaClient

    async with aiohttp.ClientSession() as session:
        report = await scan_once(
            kalshi=KalshiRestClient(session),
            gamma=PolymarketGammaClient(session),
            clob=PolymarketClobClient(session),
            sinks=[],
            limits=permissive_limits(),
            cost_assumptions=known_costs(),
            allow_unknown_fees=True,
            allow_unknown_costs=True,
            polymarket_max_markets=3000,
            polymarket_max_pages=30,
            max_kalshi_pages=8,
            now=datetime.now(UTC),
        )

    # The live universe always has cross-venue look-alikes to classify.
    assert report.kalshi_markets_scannable > 0
    assert report.poly_markets_scannable > 0
    assert report.raw_title_candidates > 0
    assert report.structured_candidates > 0

    limits = permissive_limits()
    for pair, evaluation, _reasons in report.opportunities:
        # Every accepted pair that reached economics carries its settlement
        # caveats (at minimum the ever-present UMA challenge window).
        assert pair.rule_warnings
        assert pair.status is MatchStatus.ACCEPTED
        # Any pair that actually ALERTED (no rejection reasons) must have a
        # plausibility-bounded edge — the false-match guard held on live data.
        if not _reasons:
            gross_per_share = evaluation.gross.to_dollars() / Decimal(evaluation.executable_size)
            assert gross_per_share <= limits.max_plausible_edge_per_share
