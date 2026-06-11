"""Configurable slippage models (SPEC Phase 4): fixed ¢/share, fraction of quoted
edge, and depth-derived impact."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from arb_scanner.app.types import BookLevel, Money


class SlippageModel(Protocol):
    def estimate(self, size: int, quoted_edge: Money) -> Money: ...


@dataclass(frozen=True, slots=True)
class FixedCentsSlippage:
    """size × cents_per_share."""

    cents_per_share: Decimal

    def estimate(self, size: int, quoted_edge: Money) -> Money:
        dollars = Decimal(size) * self.cents_per_share / Decimal(100)
        return Money.from_dollars(dollars.quantize(Decimal("0.000001")))


@dataclass(frozen=True, slots=True)
class EdgeFractionSlippage:
    """fraction of the quoted gross edge."""

    fraction: Decimal

    def estimate(self, size: int, quoted_edge: Money) -> Money:
        dollars = quoted_edge.to_dollars() * self.fraction
        return Money.from_dollars(dollars.quantize(Decimal("0.000001")))


@dataclass(frozen=True, slots=True)
class DepthImpactSlippage:
    """Cost of walking the book: fill cost at VWAP minus fill cost at top-of-book."""

    levels: list[BookLevel]

    def estimate(self, size: int, quoted_edge: Money) -> Money:
        if not self.levels:
            raise ValueError("no depth available")
        remaining = size
        cost = Decimal(0)
        for level in self.levels:
            take = min(remaining, level.size)
            cost += Decimal(take) * level.price
            remaining -= take
            if remaining == 0:
                break
        if remaining > 0:
            raise ValueError(f"insufficient depth: {remaining} of {size} unfilled")
        top_cost = Decimal(size) * self.levels[0].price
        return Money.from_dollars((cost - top_cost).quantize(Decimal("0.000001")))
