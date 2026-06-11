"""Economics engine: evaluate both directions of a matched pair on
depth-adjusted, fee-complete, slippage-stressed terms (SPEC Phase 4).

Both legs are BUYS: direction KALSHI_YES_POLY_NO buys Kalshi YES and the
Polymarket NO token; the pair pays $1 at resolution whichever way it resolves
(given rule equivalence), so gross = size × (1 − vwap1 − vwap2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from arb_scanner.app.books.depth import DepthResult, vwap_for_size
from arb_scanner.app.fees.kalshi import kalshi_taker_fee
from arb_scanner.app.fees.polymarket import FeeSchedule, polymarket_taker_fee
from arb_scanner.app.fees.profit import (
    FeeBreakdown,
    annualized_return,
    break_even_extra_fees,
    break_even_slippage,
    capital_locked,
    gross_profit,
    net_profit,
    simple_return,
)
from arb_scanner.app.fees.slippage import SlippageModel
from arb_scanner.app.types import Money, OrderBook


class Direction(StrEnum):
    KALSHI_YES_POLY_NO = "kalshi_yes_poly_no"
    KALSHI_NO_POLY_YES = "kalshi_no_poly_yes"


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    """Configured/live non-trading costs. `None` means unknown, never implicit zero."""

    bridge_cost: Money | None = None
    withdrawal_cost: Money | None = None
    gas_cost: Money | None = None
    processor_cost: Money | None = None
    conversion_cost: Money | None = None
    unknown_cost_buffer: Money = field(default_factory=Money.zero)

    def missing_components(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("bridge_cost", self.bridge_cost),
                ("withdrawal_cost", self.withdrawal_cost),
                ("gas_cost", self.gas_cost),
                ("processor_cost", self.processor_cost),
                ("conversion_cost", self.conversion_cost),
            )
            if value is None
        )

    def fee_breakdown(self) -> FeeBreakdown:
        return FeeBreakdown(
            bridge_cost=self.bridge_cost or Money.zero(),
            withdrawal_cost=self.withdrawal_cost or Money.zero(),
            gas_cost=self.gas_cost or Money.zero(),
            processor_cost=self.processor_cost or Money.zero(),
            conversion_cost=self.conversion_cost or Money.zero(),
            unknown_cost_buffer=self.unknown_cost_buffer,
        )


@dataclass(frozen=True, slots=True)
class OpportunityEvaluation:
    direction: Direction
    requested_size: int
    executable_size: int
    kalshi_leg: DepthResult
    poly_leg: DepthResult
    gross: Money
    fees: FeeBreakdown
    net: Money
    locked: Money
    simple_return: Decimal
    annualized_return: Decimal
    break_even_slippage_per_share: Decimal
    break_even_extra_fees: Money
    partial_fill_risk: bool

    @property
    def size(self) -> int:
        """Backward-compatible alias for the economically evaluated size."""
        return self.executable_size

    @property
    def fill_fraction(self) -> Decimal:
        return Decimal(self.executable_size) / Decimal(self.requested_size)


def evaluate_direction(
    *,
    direction: Direction,
    kalshi_view: OrderBook,
    poly_book: OrderBook,
    size: int,
    kalshi_fee_multiplier: Decimal = Decimal(1),
    poly_fee_schedule: FeeSchedule,
    slippage_model: SlippageModel,
    fixed_costs: FeeBreakdown | None = None,
    hold_days: Decimal = Decimal(30),
    fee_buffer: Money | None = None,
) -> OpportunityEvaluation | None:
    """Returns None when either leg has zero fillable depth.

    `kalshi_view` must be the view of the side being bought (its asks are hit);
    `poly_book` is the book of the token being bought. `fixed_costs` carries
    bridge/withdrawal/processor/conversion/gas from live quotes and config.
    """
    target = Decimal(size)
    kalshi_leg = vwap_for_size(kalshi_view.asks, target)
    poly_leg = vwap_for_size(poly_book.asks, target)
    if kalshi_leg.vwap is None or poly_leg.vwap is None:
        return None

    # Evaluate at the jointly fillable size: depth-adjusted, never top-of-book.
    fillable = min(kalshi_leg.fillable, poly_leg.fillable)
    eval_size = int(fillable)
    if eval_size == 0:
        return None
    if eval_size != size:
        kalshi_leg = vwap_for_size(kalshi_view.asks, Decimal(eval_size))
        poly_leg = vwap_for_size(poly_book.asks, Decimal(eval_size))
    kalshi_vwap, poly_vwap = kalshi_leg.vwap, poly_leg.vwap
    assert kalshi_vwap is not None and poly_vwap is not None  # fillable > 0 on both

    gross = gross_profit(eval_size, kalshi_vwap, poly_vwap)

    kalshi_fee = kalshi_taker_fee(eval_size, kalshi_vwap, multiplier=kalshi_fee_multiplier)
    poly_fee = polymarket_taker_fee(
        Decimal(eval_size),
        poly_vwap,
        rate=poly_fee_schedule.rate,
        exponent=poly_fee_schedule.exponent,
    )
    expected_slippage = slippage_model.estimate(eval_size, gross)

    base = fixed_costs or FeeBreakdown()
    fees = FeeBreakdown(
        kalshi_fee=kalshi_fee,
        polymarket_fee=poly_fee,
        bridge_cost=base.bridge_cost,
        withdrawal_cost=base.withdrawal_cost,
        processor_cost=base.processor_cost,
        conversion_cost=base.conversion_cost,
        gas_cost=base.gas_cost,
        expected_slippage=expected_slippage,
        unknown_cost_buffer=base.unknown_cost_buffer,
        latency_miss=base.latency_miss,
        optional_rebates=base.optional_rebates,
    )
    net = net_profit(gross, fees)

    buffer = fee_buffer if fee_buffer is not None else fees.total
    locked = capital_locked(eval_size, kalshi_vwap, poly_vwap, buffer)
    ret = simple_return(net, locked)

    return OpportunityEvaluation(
        direction=direction,
        requested_size=size,
        executable_size=eval_size,
        kalshi_leg=kalshi_leg,
        poly_leg=poly_leg,
        gross=gross,
        fees=fees,
        net=net,
        locked=locked,
        simple_return=ret,
        annualized_return=annualized_return(ret, hold_days),
        break_even_slippage_per_share=break_even_slippage(net, eval_size),
        break_even_extra_fees=break_even_extra_fees(net),
        partial_fill_risk=eval_size < size,
    )


def evaluate_both_directions(
    *,
    kalshi_yes_view: OrderBook,
    kalshi_no_view: OrderBook,
    poly_yes_book: OrderBook,
    poly_no_book: OrderBook,
    size: int,
    poly_fee_schedule: FeeSchedule,
    slippage_model: SlippageModel,
    kalshi_fee_multiplier: Decimal = Decimal(1),
    fixed_costs: FeeBreakdown | None = None,
    hold_days: Decimal = Decimal(30),
) -> list[OpportunityEvaluation]:
    results = []
    for direction, k_view, p_book in (
        (Direction.KALSHI_YES_POLY_NO, kalshi_yes_view, poly_no_book),
        (Direction.KALSHI_NO_POLY_YES, kalshi_no_view, poly_yes_book),
    ):
        evaluation = evaluate_direction(
            direction=direction,
            kalshi_view=k_view,
            poly_book=p_book,
            size=size,
            kalshi_fee_multiplier=kalshi_fee_multiplier,
            poly_fee_schedule=poly_fee_schedule,
            slippage_model=slippage_model,
            fixed_costs=fixed_costs,
            hold_days=hold_days,
        )
        if evaluation is not None:
            results.append(evaluation)
    return results
