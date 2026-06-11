"""Polymarket CLOB book parsing.

Payload shape (GET https://clob.polymarket.com/book?token_id=…, live-verified
2026-06-11, fixture tests/fixtures/live_2026-06-11/poly_book.json):
{market, asset_id, timestamp(ms str), hash, bids: [{price, size}…], asks: […]}.
The WS `book` event carries the same ladders
(https://docs.polymarket.com/market-data/websocket/market-channel).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from arb_scanner.app.types import BookLevel, OrderBook, Side, Venue


def from_clob_payload(payload: dict[str, Any], side: Side) -> OrderBook:
    """Each Polymarket token (YES/NO outcome) has its own complete book."""

    def levels(raw: list[dict[str, str]] | None) -> list[BookLevel]:
        return [BookLevel(price=Decimal(e["price"]), size=Decimal(e["size"])) for e in raw or []]

    bids = sorted(levels(payload.get("bids")), key=lambda lvl: lvl.price, reverse=True)
    asks = sorted(levels(payload.get("asks")), key=lambda lvl: lvl.price)
    ts_raw = payload.get("timestamp")
    return OrderBook(
        venue=Venue.POLYMARKET,
        market_id=payload["asset_id"],
        side=side,
        bids=tuple(bids),
        asks=tuple(asks),
        timestamp_ms=int(ts_raw) if ts_raw else None,
    )
