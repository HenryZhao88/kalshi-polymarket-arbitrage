"""Polymarket CLOB client (public reads).

Endpoints live-verified 2026-06-11 (docs/VERIFICATION.md §2). Rate limits
(docs.polymarket.com/api-reference/rate-limits): CLOB general 9,000 req/10s,
/book 1,500 req/10s — enforced client-side with sliding windows; Cloudflare
throttles rather than rejects on excess.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from arb_scanner.app.clients.base import CircuitBreaker, RestClient, SlidingWindowLimiter

CLOB_BASE_URL = "https://clob.polymarket.com"


class PolymarketClobClient:
    def __init__(self, session: aiohttp.ClientSession, *, base_url: str = CLOB_BASE_URL) -> None:
        breaker = CircuitBreaker()
        self._rest = RestClient(
            session,
            base_url,
            name="polymarket_clob",
            window=SlidingWindowLimiter(max_requests=9000, window_seconds=10),
            breaker=breaker,
        )
        # /book has its own tighter window on top of the general one
        self._book_window = SlidingWindowLimiter(max_requests=1500, window_seconds=10)

    async def get_sampling_markets(self, next_cursor: str | None = None) -> dict[str, Any]:
        params = {"next_cursor": next_cursor} if next_cursor else None
        result: dict[str, Any] = await self._rest.request_json(
            "GET", "/sampling-markets", params=params
        )
        return result

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._rest.request_json("GET", f"/markets/{condition_id}")
        return result

    async def get_book(self, token_id: str) -> dict[str, Any]:
        await self._book_window.acquire()
        result: dict[str, Any] = await self._rest.request_json(
            "GET", "/book", params={"token_id": token_id}
        )
        return result

    async def get_prices_history(
        self, token_id: str, *, interval: str = "1w", fidelity: int = 60
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._rest.request_json(
            "GET",
            "/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        return result

    async def get_orderbook_history(
        self, asset_id: str, *, start_ts: int, end_ts: int | None = None
    ) -> dict[str, Any]:
        """UNDOCUMENTED endpoint (VERIFICATION.md §2.7) — works as of 2026-06-11 but
        may change without notice; callers must tolerate failure."""
        params: dict[str, Any] = {"asset_id": asset_id, "startTs": start_ts}
        if end_ts is not None:
            params["endTs"] = end_ts
        result: dict[str, Any] = await self._rest.request_json(
            "GET", "/orderbook-history", params=params
        )
        return result

    async def get_fee_rate(self, token_id: str) -> dict[str, Any]:
        """base_fee is a protocol cap in bps, NOT the effective taker rate
        (VERIFICATION.md §2.2); never price from this alone."""
        result: dict[str, Any] = await self._rest.request_json(
            "GET", "/fee-rate", params={"token_id": token_id}
        )
        return result
