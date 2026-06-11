"""Runtime geoblock / execution-eligibility gate.

GET https://polymarket.com/api/geoblock → {blocked, ip, country, region}
(docs.polymarket.com/api-reference/geoblock, retrieved 2026-06-11). The US is
fully blocked for order placement. Execution requires BOTH
mode=execution-enabled AND blocked=false; anything else hard-disables the
execution path (SPEC prime directive 5).
"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from arb_scanner.app.clients.base import CircuitBreaker, RestClient
from arb_scanner.app.config import Mode, Settings

GEOBLOCK_BASE_URL = "https://polymarket.com"


class ExecutionDisabledError(Exception):
    """Raised whenever any execution path is touched while ineligible."""


@dataclass(frozen=True, slots=True)
class GeoblockStatus:
    blocked: bool
    country: str
    region: str


class GeoblockClient:
    def __init__(
        self, session: aiohttp.ClientSession, *, base_url: str = GEOBLOCK_BASE_URL
    ) -> None:
        self._rest = RestClient(session, base_url, name="geoblock", breaker=CircuitBreaker())

    async def check(self) -> GeoblockStatus:
        payload = await self._rest.request_json("GET", "/api/geoblock")
        return GeoblockStatus(
            blocked=bool(payload["blocked"]),
            country=str(payload.get("country", "")),
            region=str(payload.get("region", "")),
        )


async def ensure_execution_allowed(settings: Settings, geoblock: GeoblockClient) -> None:
    """The single gate every execution path must pass. Fails closed."""
    if settings.mode is not Mode.EXECUTION_ENABLED:
        raise ExecutionDisabledError(
            "mode is discovery-only; set ARB_MODE=execution-enabled explicitly"
        )
    try:
        status = await geoblock.check()
    except Exception as exc:
        raise ExecutionDisabledError(f"geoblock check failed; failing closed: {exc}") from exc
    if status.blocked:
        raise ExecutionDisabledError(
            f"Polymarket geoblock active for {status.country}/{status.region}; "
            "execution hard-disabled"
        )
