"""Polymarket Gamma client (market discovery).

Live-verified 2026-06-11 (fixture poly_gamma.json). Rate limits: Gamma general
4,000 req/10s, /markets 300 req/10s (docs.polymarket.com/api-reference/rate-limits).
"""

from __future__ import annotations

from typing import Any

import aiohttp

from arb_scanner.app.clients.base import CircuitBreaker, RestClient, SlidingWindowLimiter

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class PolymarketGammaClient:
    def __init__(self, session: aiohttp.ClientSession, *, base_url: str = GAMMA_BASE_URL) -> None:
        self._rest = RestClient(
            session,
            base_url,
            name="polymarket_gamma",
            window=SlidingWindowLimiter(max_requests=300, window_seconds=10),
            breaker=CircuitBreaker(),
        )

    async def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._rest.request_json(
            "GET",
            "/markets",
            params={"limit": limit, "offset": offset, "closed": str(closed).lower()},
        )
        return result
