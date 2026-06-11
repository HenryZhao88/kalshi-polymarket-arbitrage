"""Polymarket bridge quote client.

POST https://bridge.polymarket.com/quote
(docs.polymarket.com/api-reference/bridge/get-a-quote, retrieved 2026-06-11).
Rate limit 50 req/10s — the tightest of all Polymarket APIs; quotes are fetched
sparingly and never hardcoded.
"""

from __future__ import annotations

import aiohttp

from arb_scanner.app.clients.base import CircuitBreaker, RestClient, SlidingWindowLimiter
from arb_scanner.app.fees.bridge import BridgeQuote

BRIDGE_BASE_URL = "https://bridge.polymarket.com"


class PolymarketBridgeClient:
    def __init__(self, session: aiohttp.ClientSession, *, base_url: str = BRIDGE_BASE_URL) -> None:
        self._rest = RestClient(
            session,
            base_url,
            name="polymarket_bridge",
            window=SlidingWindowLimiter(max_requests=50, window_seconds=10),
            breaker=CircuitBreaker(),
        )

    async def get_quote(
        self,
        *,
        from_amount_base_unit: str,
        from_chain_id: str,
        from_token_address: str,
        recipient_address: str,
        to_chain_id: str,
        to_token_address: str,
    ) -> BridgeQuote:
        payload = await self._rest.request_json(
            "POST",
            "/quote",
            json_body={
                "fromAmountBaseUnit": from_amount_base_unit,
                "fromChainId": from_chain_id,
                "fromTokenAddress": from_token_address,
                "recipientAddress": recipient_address,
                "toChainId": to_chain_id,
                "toTokenAddress": to_token_address,
            },
        )
        return BridgeQuote.from_payload(payload)
