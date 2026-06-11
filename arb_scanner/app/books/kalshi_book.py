"""Kalshi binary-book normalization.

Kalshi exposes only YES and NO **bid** ladders; there are no asks in the feed.
A YES bid at price X is equivalent to a NO ask at 1−X (and vice versa), so each
side's tradable view synthesizes its ask ladder from the complementary bids.
Official statement: https://docs.kalshi.com/api-reference/market/get-market-orderbook
(retrieved 2026-06-11). Ladder element format after the fixed-point migration:
``[price_dollars_str, contract_count_fp_str]``, ascending price (best bid last).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from arb_scanner.app.types import BookLevel, OrderBook, Side, Venue

Ladder = tuple[BookLevel, ...]


def _parse_ladder(raw: list[list[str]] | None) -> Ladder:
    levels = [BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (raw or [])]
    return tuple(sorted(levels, key=lambda lvl: lvl.price, reverse=True))


def _complement(ladder: Ladder) -> Ladder:
    """Bids at X become asks at 1−X; descending bids become ascending asks."""
    return tuple(BookLevel(price=Decimal(1) - lvl.price, size=lvl.size) for lvl in ladder)


@dataclass(frozen=True, slots=True)
class KalshiBook:
    """Raw normalized state: both bid ladders, best bid first."""

    market_ticker: str
    yes_bids: Ladder
    no_bids: Ladder
    timestamp_ms: int | None = None

    @classmethod
    def from_rest_payload(
        cls, market_ticker: str, payload: dict[str, Any], *, timestamp_ms: int | None = None
    ) -> KalshiBook:
        book = payload["orderbook_fp"]
        return cls(
            market_ticker=market_ticker,
            yes_bids=_parse_ladder(book.get("yes_dollars")),
            no_bids=_parse_ladder(book.get("no_dollars")),
            timestamp_ms=timestamp_ms,
        )

    @classmethod
    def from_ws_snapshot(cls, msg: dict[str, Any]) -> KalshiBook:
        return cls(
            market_ticker=msg["market_ticker"],
            yes_bids=_parse_ladder(msg.get("yes_dollars_fp")),
            no_bids=_parse_ladder(msg.get("no_dollars_fp")),
            timestamp_ms=int(msg["ts_ms"]) if msg.get("ts_ms") is not None else None,
        )

    def view(self, side: Side) -> OrderBook:
        """Tradable view for one side: real bids + asks synthesized from the
        complementary bids."""
        own = self.yes_bids if side is Side.YES else self.no_bids
        other = self.no_bids if side is Side.YES else self.yes_bids
        return OrderBook(
            venue=Venue.KALSHI,
            market_id=self.market_ticker,
            side=side,
            bids=own,
            asks=_complement(other),
            timestamp_ms=self.timestamp_ms,
        )

    @property
    def is_crossed(self) -> bool:
        """best YES bid + best NO bid > 1 means the synthesized views cross."""
        if not self.yes_bids or not self.no_bids:
            return False
        return self.yes_bids[0].price + self.no_bids[0].price > Decimal(1)

    def apply_delta(self, price_dollars: Decimal, delta: Decimal, side: Side) -> KalshiBook:
        """Apply one WS orderbook_delta; returns a new book (original unchanged).

        Message schema: https://docs.kalshi.com/websockets/orderbook-updates.md
        (retrieved 2026-06-11): {price_dollars, delta_fp, side}.
        """
        ladder = self.yes_bids if side is Side.YES else self.no_bids
        existing = {lvl.price: lvl.size for lvl in ladder}
        new_size = existing.get(price_dollars, Decimal(0)) + delta
        if new_size < 0:
            raise ValueError(
                f"delta {delta} at {price_dollars} would make size negative "
                f"({self.market_ticker} {side})"
            )
        existing[price_dollars] = new_size
        updated = tuple(
            BookLevel(price=p, size=s) for p, s in sorted(existing.items(), reverse=True) if s > 0
        )
        if side is Side.YES:
            return replace(self, yes_bids=updated)
        return replace(self, no_bids=updated)
