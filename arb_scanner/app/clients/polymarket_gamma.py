"""Polymarket Gamma client (market discovery).

Live-verified 2026-06-11 (fixture poly_gamma.json). Rate limits: Gamma general
4,000 req/10s, /markets 300 req/10s (docs.polymarket.com/api-reference/rate-limits).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from arb_scanner.app.clients.base import (
    CircuitBreaker,
    RestClient,
    SlidingWindowLimiter,
    VenueError,
)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
log = logging.getLogger("arb_scanner.clients.polymarket_gamma")


@dataclass(frozen=True, slots=True)
class GammaDiscoveryResult:
    markets: list[dict[str, Any]]
    pages_fetched: int
    total_fetched: int


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

    async def get_markets_keyset(
        self,
        *,
        limit: int = 100,
        after_cursor: str | None = None,
        closed: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "closed": str(closed).lower()}
        if after_cursor:
            params["after_cursor"] = after_cursor
        result: dict[str, Any] = await self._rest.request_json(
            "GET", "/markets/keyset", params=params
        )
        return result

    async def get_all_markets(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 5,
        max_markets: int = 500,
        closed: bool = False,
    ) -> GammaDiscoveryResult:
        """Page through Gamma's stable keyset endpoint with bounded loop protection."""
        if not 1 <= page_size <= 100:
            raise ValueError("Gamma keyset page_size must be between 1 and 100")
        markets: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        seen_conditions: set[str] = set()
        cursor: str | None = None
        pages = 0
        total_fetched = 0

        while pages < max_pages and len(markets) < max_markets:
            page = await self.get_markets_keyset(
                limit=min(page_size, max_markets - len(markets)),
                after_cursor=cursor,
                closed=closed,
            )
            raw_markets = page.get("markets") or []
            if not isinstance(raw_markets, list):
                raise VenueError("Gamma keyset response has non-list markets field")
            pages += 1
            total_fetched += len(raw_markets)
            fingerprint = tuple(
                str(item.get("conditionId") or item.get("id") or "")
                for item in raw_markets
                if isinstance(item, dict)
            )
            if raw_markets and fingerprint in seen_pages:
                raise VenueError("Gamma pagination repeated a market page")
            seen_pages.add(fingerprint)

            for item in raw_markets:
                if not isinstance(item, dict):
                    continue
                identity = str(item.get("conditionId") or item.get("id") or "")
                if identity and identity in seen_conditions:
                    continue
                if identity:
                    seen_conditions.add(identity)
                markets.append(item)
                if len(markets) >= max_markets:
                    break

            next_cursor = str(page.get("next_cursor") or "")
            if not next_cursor:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise VenueError(f"Gamma pagination repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        log.info(
            "Gamma discovery pages=%d fetched=%d unique=%d",
            pages,
            total_fetched,
            len(markets),
        )
        return GammaDiscoveryResult(
            markets=markets,
            pages_fetched=pages,
            total_fetched=total_fetched,
        )
