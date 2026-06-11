"""End-to-end scan pass over stub clients: matched pair → depth → fees → net →
alert or rejection reason (the SPEC dry-run trail)."""

from typing import Any

from arb_scanner.app.alerts.base import AlertPayload
from arb_scanner.app.risk.controls import RiskLimits
from arb_scanner.app.scanner import scan_once
from arb_scanner.app.types import Money

KALSHI_MARKET = {
    "ticker": "KXBTCD-26JUN30-T70000",
    "title": "Bitcoin above $70,000 on June 30?",
    "expected_expiration_time": "2026-06-30T16:00:00Z",
    "can_close_early": False,
    "status": "active",
    "rules_primary": "coindesk index",
}
POLY_MARKET = {
    "conditionId": "0xabc",
    "question": "Will BTC be above $70k on June 30?",
    "endDate": "2026-06-30T16:00:00Z",
    "resolutionSource": "coindesk index",
    "clobTokenIds": '["111", "222"]',
    "tags": ["Crypto"],
}
# Kalshi YES asks come from NO bids: NO bid 0.09 → YES ask 0.91... we want a clear
# arb: YES ask 0.90 (NO bid 0.10), Poly NO ask 0.03.
KALSHI_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.0500", "500.00"]],
        "no_dollars": [["0.1000", "500.00"]],
    }
}
POLY_NO_BOOK = {
    "asset_id": "222",
    "timestamp": "1781178206890",
    "bids": [],
    "asks": [{"price": "0.03", "size": "500"}],
}
POLY_YES_BOOK = {
    "asset_id": "111",
    "timestamp": "1781178206890",
    "bids": [],
    "asks": [{"price": "0.99", "size": "500"}],
}


class StubKalshi:
    async def get_markets(self, **kwargs: Any) -> dict[str, Any]:
        return {"markets": [KALSHI_MARKET]}

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        return KALSHI_BOOK


class StubGamma:
    async def get_markets(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [POLY_MARKET]


class StubClob:
    async def get_book(self, token_id: str) -> dict[str, Any]:
        return POLY_NO_BOOK if token_id == "222" else POLY_YES_BOOK


class CapturingSink:
    def __init__(self) -> None:
        self.sent: list[AlertPayload] = []

    async def send(self, payload: AlertPayload) -> None:
        self.sent.append(payload)


class TestScanOnce:
    async def test_full_trail_produces_alert(self) -> None:
        sink = CapturingSink()
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[sink],
            limits=RiskLimits(min_net_profit=Money.from_dollars("1")),
        )
        assert report.pairs_accepted == 1
        assert report.opportunities
        # the profitable direction (Kalshi YES @ 0.90 + Poly NO @ 0.03) must alert
        alerted = [o for o in report.opportunities if not o[2]]
        assert alerted, [o[2] for o in report.opportunities]
        assert sink.sent
        assert sink.sent[0].net_edge > Money.zero()
        lines = report.render_lines()
        assert any("ALERT" in line for line in lines)

    async def test_risk_rejection_recorded_with_reason(self) -> None:
        sink = CapturingSink()
        report = await scan_once(
            kalshi=StubKalshi(),
            gamma=StubGamma(),
            clob=StubClob(),
            sinks=[sink],
            limits=RiskLimits(min_net_profit=Money.from_dollars("1000000")),
        )
        assert sink.sent == []
        assert all(reasons for _, _, reasons in report.opportunities)
        assert any("net $" in r for _, _, reasons in report.opportunities for r in reasons)
