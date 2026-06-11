"""Depth aggregation: fillable size and VWAP for a candidate order size."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb_scanner.app.types import BookLevel


@dataclass(frozen=True, slots=True)
class DepthResult:
    requested: Decimal
    fillable: Decimal
    vwap: Decimal | None  # None when nothing is fillable
    levels_consumed: int

    @property
    def is_partial(self) -> bool:
        return self.fillable < self.requested

    @property
    def fill_fraction(self) -> Decimal:
        if self.requested == 0:
            return Decimal(0)
        return self.fillable / self.requested


def vwap_for_size(asks: tuple[BookLevel, ...], size: Decimal) -> DepthResult:
    """Walk the ask ladder (ascending) and compute the volume-weighted average
    price for buying `size`; flags partial fills when depth runs out."""
    remaining = size
    cost = Decimal(0)
    consumed = 0
    for level in asks:
        if remaining <= 0:
            break
        take = min(remaining, level.size)
        cost += take * level.price
        remaining -= take
        consumed += 1
    fillable = size - remaining
    vwap = (cost / fillable) if fillable > 0 else None
    return DepthResult(requested=size, fillable=fillable, vwap=vwap, levels_consumed=consumed)
