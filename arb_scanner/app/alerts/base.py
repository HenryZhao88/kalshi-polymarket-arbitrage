"""Common alert payload and adapter protocol (SPEC Phase 5)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from arb_scanner.app.fees.profit import FeeBreakdown
from arb_scanner.app.types import Money


@dataclass(frozen=True, slots=True)
class AlertPayload:
    """One payload shared by every alert channel."""

    kalshi_ticker: str
    poly_condition_id: str
    direction: str
    confidence: float
    size: int
    depth_summary: str
    fees: FeeBreakdown
    net_edge: Money
    simple_return: Decimal
    annualized_return: Decimal
    break_even_slippage_per_share: Decimal
    break_even_extra_fees: Money
    snapshot_id: int | None

    def render_text(self) -> str:
        lines = [
            f"ARB {self.direction} | {self.kalshi_ticker} <> {self.poly_condition_id[:14]}…",
            f"confidence {self.confidence:.2f} | size {self.size} | {self.depth_summary}",
            (
                f"fees: kalshi ${self.fees.kalshi_fee.to_dollars()} "
                f"poly ${self.fees.polymarket_fee.to_dollars()} "
                f"slippage ${self.fees.expected_slippage.to_dollars()} "
                f"other ${(self.fees.total - self.fees.kalshi_fee - self.fees.polymarket_fee - self.fees.expected_slippage).to_dollars()}"
            ),
            (
                f"NET ${self.net_edge.to_dollars()} | roi {self.simple_return:.2%} "
                f"| annualized {self.annualized_return:.1%}"
            ),
            (
                f"break-even: {self.break_even_slippage_per_share * 100:.2f}c/share slippage "
                f"or ${self.break_even_extra_fees.to_dollars()} extra fees"
            ),
            f"snapshot #{self.snapshot_id}" if self.snapshot_id is not None else "no snapshot",
        ]
        return "\n".join(lines)


class AlertSink(Protocol):
    async def send(self, payload: AlertPayload) -> None: ...
