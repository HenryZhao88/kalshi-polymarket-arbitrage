"""End-to-end discovery scan tests over deterministic venue stubs."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb_scanner.app.alerts.base import AlertPayload
from arb_scanner.app.clients.polymarket_gamma import GammaDiscoveryResult
from arb_scanner.app.economics import CostAssumptions, Direction
from arb_scanner.app.markets.polymarket import PolymarketMarket
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


def test_rejection_histogram_uses_one_primary_reason() -> None:
    assert (
        _primary_rejection_bucket(("threshold 1 != 2", "similarity below review threshold"))
        == "threshold_conflict"
    )


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
        manual = {key: value for key, value in POLY_MARKET.items() if key != "voidPolicy"}
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
        assert manual_rows[0].matched_fields["missing_rule_fields"] == ["void_policy"]
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
