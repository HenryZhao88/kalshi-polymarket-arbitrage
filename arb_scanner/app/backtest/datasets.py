"""Backtest datasets: load persisted snapshots into replayable series.

Primary source is our own snapshot persistence from live scanning; the
undocumented Polymarket orderbook-history endpoint can seed history where it
exists (docs/VERIFICATION.md §2.7) via `from_orderbook_history_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from arb_scanner.app.types import BookLevel, OrderBook, Side, Venue


@dataclass(frozen=True, slots=True)
class BookFrame:
    """One timestamped book observation in a replay series."""

    captured_at: datetime
    book: OrderBook


def _levels(raw: list[Any]) -> tuple[BookLevel, ...]:
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(BookLevel(price=Decimal(entry["price"]), size=Decimal(entry["size"])))
        else:
            price, size = entry
            out.append(BookLevel(price=Decimal(price), size=Decimal(size)))
    return tuple(out)


def from_snapshot_row(
    payload: dict[str, Any], captured_at: datetime, venue: str, market_id: str
) -> BookFrame:
    """Rebuild a frame from a storage.BookSnapshotRow payload (orderbook format)."""
    bids = tuple(sorted(_levels(payload.get("bids", [])), key=lambda l: l.price, reverse=True))
    asks = tuple(sorted(_levels(payload.get("asks", [])), key=lambda l: l.price))
    return BookFrame(
        captured_at=captured_at,
        book=OrderBook(
            venue=Venue(venue),
            market_id=market_id,
            side=Side(payload.get("side", "yes")),
            bids=bids,
            asks=asks,
            timestamp_ms=payload.get("timestamp_ms"),
        ),
    )


def from_orderbook_history_payload(payload: dict[str, Any], side: Side) -> list[BookFrame]:
    """Parse the undocumented CLOB orderbook-history response into frames."""
    frames = []
    for snap in payload.get("data", []):
        ts_ms = int(snap["timestamp"])
        frames.append(
            BookFrame(
                captured_at=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                book=OrderBook(
                    venue=Venue.POLYMARKET,
                    market_id=snap["asset_id"],
                    side=side,
                    bids=tuple(
                        sorted(_levels(snap.get("bids", [])), key=lambda l: l.price, reverse=True)
                    ),
                    asks=tuple(sorted(_levels(snap.get("asks", [])), key=lambda l: l.price)),
                    timestamp_ms=ts_ms,
                ),
            )
        )
    frames.sort(key=lambda f: f.captured_at)
    return frames
