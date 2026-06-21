"""Kalshi REST client + RSA-PSS request signing.

Auth scheme (official starter code, github.com/Kalshi/kalshi-starter-code-python,
retrieved 2026-06-11): sign `timestamp_ms + METHOD + path` with RSA-PSS/SHA-256
(salt length = digest length), base64; send KALSHI-ACCESS-{KEY,SIGNATURE,TIMESTAMP}.
Public market-data reads (markets, orderbook, fee changes) work unauthenticated —
live-verified 2026-06-11 (docs/VERIFICATION.md §1.4).

Rate limits: token bucket, Basic tier 200 read tokens/s (most requests cost 10);
no Retry-After on 429 (docs.kalshi.com/getting_started/rate_limits).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arb_scanner.app.clients.base import CircuitBreaker, RestClient, TokenBucket, VenueError

PROD_BASE_URL = "https://api.elections.kalshi.com"
DEMO_BASE_URL = "https://demo-api.kalshi.co"
API_PREFIX = "/trade-api/v2"

log = logging.getLogger("arb_scanner.clients.kalshi_rest")


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes) -> None:
        self._key_id = key_id
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi API keys are RSA private keys")
        self._private_key: rsa.RSAPrivateKey = key

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        """`path` is the full request path including the /trade-api/v2 prefix."""
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }


class KalshiRestClient:
    """Market-data reads (unauthenticated) with optional signer for private paths."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str = PROD_BASE_URL,
        signer: KalshiSigner | None = None,
        read_bucket: TokenBucket | None = None,
    ) -> None:
        self._signer = signer
        self._rest = RestClient(
            session,
            base_url,
            name="kalshi",
            bucket=read_bucket or TokenBucket(refill_rate=200, capacity=200),
            breaker=CircuitBreaker(),
        )

    def _headers(self, method: str, path: str) -> dict[str, str] | None:
        return self._signer.headers(method, path) if self._signer else None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        full_path = f"{API_PREFIX}{path}"
        return await self._rest.request_json(
            "GET", full_path, params=params, headers=self._headers("GET", full_path)
        )

    async def get_exchange_status(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._get("/exchange/status")
        return result

    async def get_markets(
        self,
        *,
        limit: int = 100,
        status: str | None = "open",
        series_ticker: str | None = None,
        cursor: str | None = None,
        mve_filter: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        if mve_filter:
            params["mve_filter"] = mve_filter
        result: dict[str, Any] = await self._get("/markets", params)
        return result

    async def get_all_markets(
        self,
        *,
        limit: int = 1000,
        status: str | None = "open",
        series_ticker: str | None = None,
        mve_filter: str | None = "exclude",
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Return all pages, failing only on a repeated cursor (loop bug).

        Reaching ``max_pages`` is a graceful stop that returns what was
        collected — the cap is a safety guardrail for full-venue coverage, not
        a scan-killing error. Raise ``max_pages`` to cover a larger universe.
        """
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            page = await self.get_markets(
                limit=limit,
                status=status,
                series_ticker=series_ticker,
                cursor=cursor,
                mve_filter=mve_filter,
            )
            markets.extend(page.get("markets", []))
            next_cursor = str(page.get("cursor") or "")
            if not next_cursor:
                return markets
            if next_cursor in seen_cursors:
                raise VenueError(f"Kalshi pagination repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        log.warning(
            "Kalshi discovery stopped at the %d-page cap with %d markets; more "
            "remain unfetched (raise kalshi_max_pages to cover them)",
            max_pages,
            len(markets),
        )
        return markets

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        params = {"depth": depth} if depth is not None else None
        result: dict[str, Any] = await self._get(f"/markets/{ticker}/orderbook", params)
        return result

    async def get_series_fee_changes(self, show_historical: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = await self._get(
            "/series/fee_changes", {"show_historical": str(show_historical).lower()}
        )
        return result
