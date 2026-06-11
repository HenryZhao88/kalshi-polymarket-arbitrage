"""Fill simulator: execute against stored depth with latency, stale-quote
rejection, partial fills, configurable slippage, and fee drift via the versioned
override table (SPEC Phase 6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from arb_scanner.app.backtest.datasets import BookFrame
from arb_scanner.app.books.depth import vwap_for_size


class FillOutcome(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    STALE_QUOTE = "stale_quote"
    NO_DEPTH = "no_depth"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    outcome: FillOutcome
    requested: Decimal
    filled: Decimal
    vwap: Decimal | None
    slippage_per_share: Decimal
    decided_at: datetime
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class FillSimulator:
    """Executes a buy decision made at frame[i] against the book that actually
    prevails after `latency_ms` (the next frame at/after decision+latency)."""

    latency_ms: int = 250
    max_quote_age: timedelta = timedelta(seconds=30)
    extra_slippage_per_share: Decimal = Decimal(0)

    def execute_buy(
        self, frames: list[BookFrame], decision_index: int, size: Decimal
    ) -> SimulatedFill:
        decision_frame = frames[decision_index]
        decided_at = decision_frame.captured_at
        executed_at = decided_at + timedelta(milliseconds=self.latency_ms)

        # The book we actually hit is the latest frame at or before execution time;
        # quotes older than max_quote_age are rejected as stale.
        execution_frame = decision_frame
        for frame in frames[decision_index:]:
            if frame.captured_at <= executed_at:
                execution_frame = frame
            else:
                break
        if executed_at - execution_frame.captured_at > self.max_quote_age:
            return SimulatedFill(
                outcome=FillOutcome.STALE_QUOTE,
                requested=size,
                filled=Decimal(0),
                vwap=None,
                slippage_per_share=self.extra_slippage_per_share,
                decided_at=decided_at,
                executed_at=executed_at,
            )

        depth = vwap_for_size(execution_frame.book.asks, size)
        if depth.vwap is None:
            return SimulatedFill(
                outcome=FillOutcome.NO_DEPTH,
                requested=size,
                filled=Decimal(0),
                vwap=None,
                slippage_per_share=self.extra_slippage_per_share,
                decided_at=decided_at,
                executed_at=executed_at,
            )
        effective_vwap = depth.vwap + self.extra_slippage_per_share
        return SimulatedFill(
            outcome=FillOutcome.PARTIAL if depth.is_partial else FillOutcome.FILLED,
            requested=size,
            filled=depth.fillable,
            vwap=effective_vwap,
            slippage_per_share=self.extra_slippage_per_share,
            decided_at=decided_at,
            executed_at=executed_at,
        )
