"""Normalized Gamma market metadata tests."""

from decimal import Decimal

from arb_scanner.app.markets.polymarket import PolymarketMarket


def test_normalizes_discovery_fields() -> None:
    market = PolymarketMarket.from_gamma(
        {
            "id": "42",
            "question": "Will the event happen?",
            "conditionId": "0xabc",
            "clobTokenIds": '["yes", "no"]',
            "outcomes": '["Yes", "No"]',
            "endDate": "2026-07-01T00:00:00Z",
            "umaEndDate": "2026-07-03T00:00:00Z",
            "category": "Politics",
            "description": "Rules text",
            "resolutionSource": "Associated Press",
            "feeSchedule": {"rate": "0.05", "exponent": 1, "takerOnly": True},
            "active": True,
            "closed": False,
            "archived": False,
            "acceptingOrders": True,
            "enableOrderBook": True,
            "liquidityNum": 123.45,
            "volumeNum": 678.9,
        }
    )
    assert market.question == "Will the event happen?"
    assert market.token_ids == ("yes", "no")
    assert market.outcomes == ("Yes", "No")
    assert market.category == "politics"
    assert market.fee_schedule is not None and market.fee_schedule.rate == Decimal("0.05")
    assert market.liquidity == Decimal("123.45")
    assert market.volume == Decimal("678.9")
    assert market.scannable


def test_closed_market_is_not_scannable() -> None:
    market = PolymarketMarket.from_gamma(
        {
            "question": "Closed?",
            "conditionId": "0xclosed",
            "clobTokenIds": '["yes", "no"]',
            "active": True,
            "closed": True,
        }
    )
    assert not market.scannable
