"""Profit composition: gross/net P&L, capital efficiency, returns (SPEC Phase 4).

Pure functions; the economics engine composes these with live books and fee outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from arb_scanner.app.types import Money


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Every cost component of a two-leg opportunity, itemized.

    `optional_rebates` (Polymarket maker/taker rebate programs) is informational
    only and deliberately excluded from `total` — rebates never improve the
    headline edge (SPEC Phase 1).
    """

    kalshi_fee: Money = field(default_factory=Money.zero)
    polymarket_fee: Money = field(default_factory=Money.zero)
    bridge_cost: Money = field(default_factory=Money.zero)
    withdrawal_cost: Money = field(default_factory=Money.zero)
    processor_cost: Money = field(default_factory=Money.zero)
    conversion_cost: Money = field(default_factory=Money.zero)
    gas_cost: Money = field(default_factory=Money.zero)
    expected_slippage: Money = field(default_factory=Money.zero)
    latency_miss: Money = field(default_factory=Money.zero)
    optional_rebates: Money = field(default_factory=Money.zero)

    @property
    def total(self) -> Money:
        return (
            self.kalshi_fee
            + self.polymarket_fee
            + self.bridge_cost
            + self.withdrawal_cost
            + self.processor_cost
            + self.conversion_cost
            + self.gas_cost
            + self.expected_slippage
            + self.latency_miss
        )


def gross_profit(size: int, leg1_price_vwap: Decimal, leg2_price_vwap: Decimal) -> Money:
    """gross = size × (1 − leg1_vwap − leg2_vwap), both legs bought."""
    dollars = Decimal(size) * (Decimal(1) - leg1_price_vwap - leg2_price_vwap)
    return Money.from_dollars(dollars.quantize(Decimal("0.000001")))


def net_profit(gross: Money, fees: FeeBreakdown) -> Money:
    return gross - fees.total


def capital_locked(size: int, leg1_price: Decimal, leg2_price: Decimal, fee_buffer: Money) -> Money:
    """locked = size×p1 + size×p2 + fee_buffer (cross-venue, no collateral netting)."""
    dollars = Decimal(size) * (leg1_price + leg2_price)
    return Money.from_dollars(dollars.quantize(Decimal("0.000001"))) + fee_buffer


def simple_return(net: Money, locked: Money) -> Decimal:
    if locked <= Money.zero():
        raise ValueError("locked capital must be positive")
    return net.to_dollars() / locked.to_dollars()


def annualized_return(simple: Decimal, hold_days: Decimal) -> Decimal:
    """simple × (365 / hold_days); hold includes withdrawal-hold capital-lock time."""
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")
    return simple * Decimal(365) / hold_days


def break_even_slippage(net: Money, size: int) -> Decimal:
    """Adverse execution per share (in dollars) that zeroes the net edge."""
    if size <= 0:
        raise ValueError("size must be positive")
    return net.to_dollars() / Decimal(size)


def break_even_extra_fees(net: Money) -> Money:
    """Additional flat fees that zero the net edge."""
    return net
