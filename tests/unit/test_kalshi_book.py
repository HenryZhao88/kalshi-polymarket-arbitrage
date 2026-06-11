"""Kalshi binary-book normalization tests — the most common implementation bug.

Kalshi returns YES and NO **bid ladders only**; asks are synthesized from the
complementary side (YES bid at X ≡ NO ask at 1−X). Official confirmation:
https://docs.kalshi.com/api-reference/market/get-market-orderbook ("It returns yes
bids and no bids only (no asks are returned)"), retrieved 2026-06-11.

REST ladder fixture captured live 2026-06-11
(tests/fixtures/live_2026-06-11/kalshi_orderbook_nonempty.json).
"""

from decimal import Decimal

import pytest

from arb_scanner.app.books.kalshi_book import KalshiBook
from arb_scanner.app.types import Side

D = Decimal

# Live REST shape: ascending price, best bid LAST, [price_dollars, size_fp] strings.
REST_PAYLOAD = {
    "orderbook_fp": {
        "yes_dollars": [["0.2200", "252.00"], ["0.2300", "28.55"], ["0.2400", "100000.00"]],
        "no_dollars": [["0.7300", "40.34"], ["0.7400", "200.00"], ["0.7500", "40.00"]],
    }
}

# WS snapshot shape (docs.kalshi.com/websockets/orderbook-updates.md, verbatim example)
WS_SNAPSHOT_MSG = {
    "market_ticker": "FED-23DEC-T3.00",
    "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
    "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
    "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
}


class TestParsing:
    def test_rest_payload_best_bids(self) -> None:
        book = KalshiBook.from_rest_payload("T", REST_PAYLOAD)
        assert book.yes_bids[0].price == D("0.24")
        assert book.yes_bids[0].size == D("100000.00")
        assert book.no_bids[0].price == D("0.75")

    def test_ws_snapshot_payload(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        assert book.market_ticker == "FED-23DEC-T3.00"
        assert book.yes_bids[0].price == D("0.22")
        assert book.no_bids[0].price == D("0.56")

    def test_empty_book(self) -> None:
        book = KalshiBook.from_rest_payload(
            "T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        )
        assert book.yes_bids == ()
        assert book.no_bids == ()

    def test_null_ladders_treated_as_empty(self) -> None:
        book = KalshiBook.from_rest_payload(
            "T", {"orderbook_fp": {"yes_dollars": None, "no_dollars": None}}
        )
        assert book.yes_bids == ()


class TestNormalization:
    """YES bid at X ≡ NO ask at 1−X, and vice versa."""

    def test_yes_view_asks_synthesized_from_no_bids(self) -> None:
        view = KalshiBook.from_rest_payload("T", REST_PAYLOAD).view(Side.YES)
        # best NO bid 0.75 → best YES ask 0.25
        assert view.best_ask is not None
        assert view.best_ask.price == D("0.25")
        assert view.best_ask.size == D("40.00")
        # asks ascending: 0.25, 0.26, 0.27
        assert [lvl.price for lvl in view.asks] == [D("0.25"), D("0.26"), D("0.27")]

    def test_no_view_asks_synthesized_from_yes_bids(self) -> None:
        view = KalshiBook.from_rest_payload("T", REST_PAYLOAD).view(Side.NO)
        # best YES bid 0.24 → best NO ask 0.76
        assert view.best_ask is not None
        assert view.best_ask.price == D("0.76")
        assert [lvl.price for lvl in view.asks] == [D("0.76"), D("0.77"), D("0.78")]

    def test_sizes_preserved_through_synthesis(self) -> None:
        view = KalshiBook.from_rest_payload("T", REST_PAYLOAD).view(Side.NO)
        assert view.asks[0].size == D("100000.00")  # the 0.24 YES bid

    def test_views_are_mutually_consistent(self) -> None:
        book = KalshiBook.from_rest_payload("T", REST_PAYLOAD)
        yes, no = book.view(Side.YES), book.view(Side.NO)
        assert yes.best_bid is not None and no.best_ask is not None
        assert yes.best_bid.price + no.best_ask.price == D(1)
        assert yes.best_bid.size == no.best_ask.size

    def test_price_edges_1c_and_99c(self) -> None:
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.0100", "10.00"]],
                "no_dollars": [["0.0100", "5.00"]],
            }
        }
        view = KalshiBook.from_rest_payload("T", payload).view(Side.YES)
        assert view.best_bid is not None and view.best_bid.price == D("0.01")
        assert view.best_ask is not None and view.best_ask.price == D("0.99")

    def test_subpenny_prices_survive(self) -> None:
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.0550", "1.00"]],
                "no_dollars": [["0.9401", "2.00"]],
            }
        }
        view = KalshiBook.from_rest_payload("T", payload).view(Side.YES)
        assert view.best_bid is not None and view.best_bid.price == D("0.0550")
        assert view.best_ask is not None and view.best_ask.price == D("0.0599")

    def test_not_crossed(self) -> None:
        book = KalshiBook.from_rest_payload("T", REST_PAYLOAD)
        assert not book.is_crossed

    def test_crossed_book_detected(self) -> None:
        # YES bid 0.30 + NO bid 0.75 = 1.05 > 1 → YES bid 0.30 vs YES ask 0.25: crossed
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.3000", "1.00"]],
                "no_dollars": [["0.7500", "1.00"]],
            }
        }
        assert KalshiBook.from_rest_payload("T", payload).is_crossed


class TestDeltas:
    """WS orderbook_delta: {price_dollars, delta_fp, side} applied to a ladder."""

    def test_delta_adds_new_level(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        updated = book.apply_delta(price_dollars=D("0.10"), delta=D("50.00"), side=Side.YES)
        assert any(lvl.price == D("0.10") and lvl.size == D("50") for lvl in updated.yes_bids)

    def test_delta_increases_existing_level(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        updated = book.apply_delta(price_dollars=D("0.22"), delta=D("67.00"), side=Side.YES)
        assert updated.yes_bids[0].size == D("400.00")

    def test_negative_delta_removes_level_at_zero(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        updated = book.apply_delta(price_dollars=D("0.54"), delta=D("-20.00"), side=Side.NO)
        assert all(lvl.price != D("0.54") for lvl in updated.no_bids)

    def test_delta_below_zero_raises(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        with pytest.raises(ValueError, match="negative"):
            book.apply_delta(price_dollars=D("0.54"), delta=D("-25.00"), side=Side.NO)

    def test_original_book_is_unchanged(self) -> None:
        book = KalshiBook.from_ws_snapshot(WS_SNAPSHOT_MSG)
        book.apply_delta(price_dollars=D("0.10"), delta=D("50.00"), side=Side.YES)
        assert len(book.yes_bids) == 2
