"""Shared domain types.

Money is integer micro-dollars (1 dollar = 1_000_000 micros) so that both venues'
precision regimes are exact: Kalshi fill-exact fees round to $0.0001 (100 micros)
and Polymarket's minimum fee is $0.00001 (10 micros). Floats never enter fee math.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Self

MICROS_PER_DOLLAR = 1_000_000
MICROS_PER_CENT = 10_000


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """Immutable monetary amount in integer micro-dollars. May be negative."""

    micros: int

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    @classmethod
    def from_dollars(cls, value: Decimal | str | int) -> Self:
        as_decimal = Decimal(value) if not isinstance(value, Decimal) else value
        scaled = as_decimal * MICROS_PER_DOLLAR
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{as_decimal} is finer than micro-dollar precision")
        return cls(int(scaled))

    @classmethod
    def from_cents(cls, cents: int) -> Self:
        return cls(cents * MICROS_PER_CENT)

    def to_dollars(self) -> Decimal:
        return Decimal(self.micros) / MICROS_PER_DOLLAR

    def __add__(self, other: Money) -> Money:
        return Money(self.micros + other.micros)

    def __sub__(self, other: Money) -> Money:
        return Money(self.micros - other.micros)

    def __mul__(self, factor: int) -> Money:
        return Money(self.micros * factor)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.micros)

    def ceil_to(self, quantum_dollars: Decimal) -> Money:
        """Round up (toward +infinity) to the nearest multiple of `quantum_dollars`."""
        quantum = Money.from_dollars(quantum_dollars).micros
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        return Money(-(-self.micros // quantum) * quantum)

    def ceil_to_cent(self) -> Money:
        return self.ceil_to(Decimal("0.01"))

    def floor_to(self, quantum_dollars: Decimal) -> Money:
        """Round down (toward -infinity) to the nearest multiple of `quantum_dollars`."""
        quantum = Money.from_dollars(quantum_dollars).micros
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        return Money(self.micros // quantum * quantum)


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def complement(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One price level of a binary-contract book. Price is a probability in [0, 1]."""

    price: Decimal
    size: int

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError(f"price {self.price} outside [0, 1]")
        if self.size < 0:
            raise ValueError(f"size {self.size} is negative")
