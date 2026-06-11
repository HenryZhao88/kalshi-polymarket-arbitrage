"""Bridge/deposit cost model for Polymarket.

Costs are NEVER hardcoded: a `BridgeQuote` can only be built from a live response of
POST https://bridge.polymarket.com/quote
(https://docs.polymarket.com/api-reference/bridge/get-a-quote, retrieved 2026-06-11).
Bridge API rate limit: 50 req / 10 s — quotes should be cached upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arb_scanner.app.types import Money


def _usd(value: float | int | str) -> Money:
    return Money.from_dollars(Decimal(str(value)).quantize(Decimal("0.000001")))


@dataclass(frozen=True, slots=True)
class BridgeQuote:
    """Parsed `estFeeBreakdown` of one live bridge quote."""

    quote_id: str
    gas: Money
    app_fee: Money
    fill_cost: Money
    swap_impact: Money
    total_impact_reported: Money
    max_slippage: Decimal
    est_input: Money
    est_output: Money
    est_checkout_time_ms: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BridgeQuote:
        breakdown = payload["estFeeBreakdown"]
        return cls(
            quote_id=payload["quoteId"],
            gas=_usd(breakdown["gasUsd"]),
            app_fee=_usd(breakdown["appFeeUsd"]),
            fill_cost=_usd(breakdown["fillCostUsd"]),
            swap_impact=_usd(breakdown["swapImpactUsd"]),
            total_impact_reported=_usd(breakdown["totalImpactUsd"]),
            max_slippage=Decimal(str(breakdown["maxSlippage"])),
            est_input=_usd(payload["estInputUsd"]),
            est_output=_usd(payload["estOutputUsd"]),
            est_checkout_time_ms=int(payload["estCheckoutTimeMs"]),
        )

    @property
    def total_cost(self) -> Money:
        """Itemized component sum (gas + app fee + fill cost + swap impact)."""
        return self.gas + self.app_fee + self.fill_cost + self.swap_impact
