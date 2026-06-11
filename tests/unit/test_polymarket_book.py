"""Polymarket CLOB book parsing tests (payload shape live-verified 2026-06-11)."""

import json
from decimal import Decimal
from pathlib import Path

from arb_scanner.app.books.polymarket_book import from_clob_payload
from arb_scanner.app.types import Side, Venue

FIXTURE = Path("tests/fixtures/live_2026-06-11/poly_book.json")


class TestFromClobPayload:
    def test_parses_live_fixture(self) -> None:
        payload = json.loads(FIXTURE.read_text())
        book = from_clob_payload(payload, side=Side.YES)
        assert book.venue is Venue.POLYMARKET
        assert book.market_id == payload["asset_id"]
        assert book.timestamp_ms is not None
        assert book.best_bid is not None
        assert book.bids[0].price > book.bids[-1].price  # descending

    def test_bids_sorted_descending_asks_ascending(self) -> None:
        payload = {
            "asset_id": "tok",
            "timestamp": "1781178206890",
            "bids": [{"price": "0.01", "size": "10"}, {"price": "0.05", "size": "5"}],
            "asks": [{"price": "0.99", "size": "1"}, {"price": "0.90", "size": "2"}],
        }
        book = from_clob_payload(payload, side=Side.NO)
        assert book.best_bid is not None and book.best_bid.price == Decimal("0.05")
        assert book.best_ask is not None and book.best_ask.price == Decimal("0.90")

    def test_empty_sides(self) -> None:
        book = from_clob_payload({"asset_id": "tok", "timestamp": None}, side=Side.YES)
        assert book.bids == () and book.asks == ()
        assert book.timestamp_ms is None
