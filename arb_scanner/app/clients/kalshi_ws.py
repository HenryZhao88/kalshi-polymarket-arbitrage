"""Kalshi orderbook WebSocket: subscription plumbing + message handling.

Channel `orderbook_delta` sends `orderbook_snapshot` then incremental
`orderbook_delta` messages with a per-subscription `seq`; a gap means missed
messages and requires a fresh snapshot
(https://docs.kalshi.com/websockets/orderbook-updates.md, retrieved 2026-06-11).
WS URL: wss://api.elections.kalshi.com/trade-api/ws/v2 (official starter code).
Auth uses the same RSA-PSS headers as REST, signed for path /trade-api/ws/v2.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets

from arb_scanner.app.books.kalshi_book import KalshiBook
from arb_scanner.app.clients.kalshi_rest import KalshiSigner
from arb_scanner.app.types import Side

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


class SequenceGapError(Exception):
    """seq jumped — book state is unreliable until a new snapshot arrives."""


@dataclass
class KalshiBookTracker:
    """Maintains per-market books from snapshot/delta messages; detects seq gaps."""

    books: dict[str, KalshiBook]
    last_seq: int | None = None

    def __init__(self) -> None:
        self.books = {}
        self.last_seq = None

    def handle(self, message: dict[str, Any]) -> KalshiBook | None:
        msg_type = message.get("type")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        seq = int(message["seq"])
        if msg_type == "orderbook_snapshot":
            self.last_seq = seq
            book = KalshiBook.from_ws_snapshot(message["msg"])
            self.books[book.market_ticker] = book
            return book
        if self.last_seq is not None and seq != self.last_seq + 1:
            raise SequenceGapError(f"expected seq {self.last_seq + 1}, got {seq}")
        self.last_seq = seq
        body = message["msg"]
        ticker = body["market_ticker"]
        current = self.books.get(ticker)
        if current is None:
            raise SequenceGapError(f"delta for {ticker} before any snapshot")
        updated = current.apply_delta(
            price_dollars=Decimal(body["price_dollars"]),
            delta=Decimal(body["delta_fp"]),
            side=Side(body["side"]),
        )
        self.books[ticker] = updated
        return updated


def subscribe_command(market_tickers: list[str], command_id: int = 1) -> str:
    return json.dumps(
        {
            "id": command_id,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": market_tickers},
        }
    )


async def stream_books(
    market_tickers: list[str],
    on_book: Callable[[KalshiBook], None],
    *,
    signer: KalshiSigner | None = None,
    url: str = WS_URL,
) -> None:
    """Connect, subscribe, and forward maintained books; resubscribes on seq gaps."""
    headers = signer.headers("GET", WS_PATH) if signer else {}
    while True:
        tracker = KalshiBookTracker()
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(subscribe_command(market_tickers))
            try:
                async for raw in ws:
                    book = tracker.handle(json.loads(raw))
                    if book is not None:
                        on_book(book)
            except SequenceGapError:
                continue  # reconnect with a fresh snapshot
