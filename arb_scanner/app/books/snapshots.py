"""Book snapshot capture: serialize live books for persistence and replay.

Snapshots from live scanning are the primary historical book source for
backtesting (SPEC Phase 6; the undocumented Polymarket orderbook-history endpoint
is opportunistic only — docs/VERIFICATION.md §2.7).
"""

from __future__ import annotations

from typing import Any

from arb_scanner.app.books.kalshi_book import KalshiBook
from arb_scanner.app.types import BookLevel, OrderBook


def _ladder_payload(levels: tuple[BookLevel, ...]) -> list[list[str]]:
    return [[str(lvl.price), str(lvl.size)] for lvl in levels]


def kalshi_snapshot_payload(book: KalshiBook) -> dict[str, Any]:
    return {
        "format": "kalshi_bid_ladders",
        "market_ticker": book.market_ticker,
        "yes_bids": _ladder_payload(book.yes_bids),
        "no_bids": _ladder_payload(book.no_bids),
        "timestamp_ms": book.timestamp_ms,
    }


def orderbook_snapshot_payload(book: OrderBook) -> dict[str, Any]:
    return {
        "format": "orderbook",
        "venue": book.venue.value,
        "market_id": book.market_id,
        "side": book.side.value,
        "bids": _ladder_payload(book.bids),
        "asks": _ladder_payload(book.asks),
        "timestamp_ms": book.timestamp_ms,
    }
