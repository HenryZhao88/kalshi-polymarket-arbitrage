"""Kalshi fee functions. Pure; all monetary returns are `Money`.

Sources (retrieved 2026-06-11, see docs/VERIFICATION.md §1):
- Schedule: https://kalshi.com/fee-schedule and
  https://help.kalshi.com/en/articles/13823805-fees
- Fill-exact rounding: https://docs.kalshi.com/getting_started/fee_rounding
- Funding: https://help.kalshi.com/en/articles/13823795-card-deposits
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from arb_scanner.app.types import Money

#: General schedule coefficients (https://kalshi.com/fee-schedule, 2026-06-11).
TAKER_COEFFICIENT = Decimal("0.07")
MAKER_COEFFICIENT = Decimal("0.0175")

#: Fill-exact model: trade fee is rounded UP to the nearest centicent
#: (https://docs.kalshi.com/getting_started/fee_rounding).
TRADE_FEE_QUANTUM = Decimal("0.0001")

#: Rebates are "always a multiple of $0.01" (same source).
REBATE_QUANTUM = Decimal("0.01")


def _validate_price(price: Decimal) -> None:
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError(f"price {price} outside [0, 1]")


def kalshi_fee_raw(
    contracts: Decimal, price: Decimal, coefficient: Decimal, multiplier: Decimal = Decimal(1)
) -> Decimal:
    """Unrounded quadratic fee in dollars: multiplier × coefficient × C × P × (1−P).

    Formula: https://kalshi.com/fee-schedule (retrieved 2026-06-11). The per-series
    `multiplier` comes from GET /series/fee_changes (see fees/overrides.py).
    Worked example: C=100, P=0.50 → 0.07 × 100 × 0.25 = $1.75.
    """
    _validate_price(price)
    return multiplier * coefficient * contracts * price * (Decimal(1) - price)


def kalshi_taker_fee(contracts: int, price: Decimal, multiplier: Decimal = Decimal(1)) -> Money:
    """Coarse-schedule taker fee: ceil_to_cent(0.07 × C × P × (1−P)).

    Charged only on immediately-matched orders.
    Source: https://kalshi.com/fee-schedule (retrieved 2026-06-11).
    Worked example: C=1, P=0.50 → 0.0175 → $0.02 after cent ceiling.
    """
    raw = kalshi_fee_raw(Decimal(contracts), price, TAKER_COEFFICIENT, multiplier)
    return _ceil_raw_to_cent(raw)


def kalshi_maker_fee(contracts: int, price: Decimal, multiplier: Decimal = Decimal(1)) -> Money:
    """Coarse-schedule maker fee: ceil_to_cent(0.0175 × C × P × (1−P)).

    Applies only on series with maker fees (fee_type quadratic_with_maker_fees);
    no fee on canceled resting orders.
    Source: https://kalshi.com/fee-schedule (retrieved 2026-06-11).
    Worked example: C=100, P=0.50 → 0.4375 → $0.44.
    """
    raw = kalshi_fee_raw(Decimal(contracts), price, MAKER_COEFFICIENT, multiplier)
    return _ceil_raw_to_cent(raw)


def _ceil_raw_to_cent(raw_dollars: Decimal) -> Money:
    quantized = raw_dollars.quantize(Decimal("0.000001"))  # clamp to micro precision
    return Money.from_dollars(quantized).ceil_to_cent()


@dataclass(frozen=True, slots=True)
class FillFees:
    """Fee components of one fill: net = trade + rounding − rebate (≥ $0)."""

    trade_fee: Money
    rounding_fee: Money
    rebate: Money

    @property
    def net_fee(self) -> Money:
        return self.trade_fee + self.rounding_fee - self.rebate


@dataclass(slots=True)
class KalshiFillFeeAccumulator:
    """Fill-exact fee model with the cross-fill rounding accumulator.

    Mechanics (https://docs.kalshi.com/getting_started/fee_rounding, 2026-06-11):
    1. Round the model trade fee UP to the nearest $0.0001.
    2. balance_change = −notional − trade_fee, floored (toward −inf) to the member's
       balance precision ($0.01 non-direct, $0.0001 direct members).
    3. rounding_fee = balance_change − floor(balance_change).
    4. Rounding overpayment accumulates across fills of the order; once it exceeds
       $0.01 a whole-cent rebate is issued and the accumulator reduced accordingly.
    Validated against the page's three worked examples in tests/unit/test_fees_kalshi.py.
    """

    balance_precision: Decimal = Decimal("0.01")
    accumulated: Money = field(default_factory=Money.zero)

    def apply_fill(self, notional: Money, trade_fee_raw: Decimal) -> FillFees:
        trade_fee = Money.from_dollars(trade_fee_raw.quantize(Decimal("0.000001"))).ceil_to(
            TRADE_FEE_QUANTUM
        )

        balance_change = -notional - trade_fee
        floored = balance_change.floor_to(self.balance_precision)
        rounding_fee = balance_change - floored

        self.accumulated = self.accumulated + rounding_fee
        rebate = Money.zero()
        if self.accumulated > Money.from_dollars(REBATE_QUANTUM):
            rebate = self.accumulated.floor_to(REBATE_QUANTUM)
            self.accumulated = self.accumulated - rebate

        return FillFees(trade_fee=trade_fee, rounding_fee=rounding_fee, rebate=rebate)


def debit_deposit_fee(amount: Money, rate: Decimal = Decimal("0.02")) -> Money:
    """Debit-card deposit processing fee, up to 2%, rounded up to the cent.

    Source: https://help.kalshi.com/en/articles/13823795-card-deposits (2026-06-11):
    "Debit deposits may incur a 2% processing fee." ACH/bank and wire deposits are
    free. Cent ceiling is our conservative assumption; the help page does not state
    a rounding rule. Withdrawal holds are modeled as capital-lock time (config), not
    a dollar cost.
    Worked example: $100 deposit → $2.00.
    """
    raw = amount.to_dollars() * rate
    return Money.from_dollars(raw.quantize(Decimal("0.000001"))).ceil_to_cent()
