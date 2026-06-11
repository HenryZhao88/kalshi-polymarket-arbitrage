"""Execution simulator — the only 'execution' shipped by default.

Routes a two-leg opportunity through the backtest FillSimulator instead of any
venue order API. Real order routing (router.py / orders.py) is hard-disabled
behind the geoblock gate (clients/geoblock.ensure_execution_allowed).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb_scanner.app.backtest.datasets import BookFrame
from arb_scanner.app.backtest.fills import FillSimulator, SimulatedFill


@dataclass(frozen=True, slots=True)
class TwoLegSimulation:
    leg1: SimulatedFill
    leg2: SimulatedFill

    @property
    def both_filled(self) -> bool:
        return self.leg1.filled > 0 and self.leg2.filled > 0

    @property
    def matched_size(self) -> Decimal:
        return min(self.leg1.filled, self.leg2.filled)


def simulate_two_leg(
    leg1_frames: list[BookFrame],
    leg2_frames: list[BookFrame],
    *,
    decision_index: int,
    size: Decimal,
    simulator: FillSimulator | None = None,
) -> TwoLegSimulation:
    simulator = simulator or FillSimulator()
    return TwoLegSimulation(
        leg1=simulator.execute_buy(leg1_frames, decision_index, size),
        leg2=simulator.execute_buy(leg2_frames, decision_index, size),
    )
