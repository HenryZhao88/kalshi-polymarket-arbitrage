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
    #: Settlement-mechanic caveats a human must verify before trading (same
    #: event, different tail-state handling): resolution source, void policy,
    #: UMA challenge window, close-timestamp differences, etc. An alert is a
    #: price-dislocation lead on a likely-equivalent pair, never a guaranteed
    #: profit — these flags say what to check.
    risk_flags: tuple[str, ...] = ()
    #: Automated-verifier verdict on the risk flags: "verified" (no human action
    #: needed), "needs_human" (check `unresolved_flags`), or None when the
    #: verifier did not run. A "rejected" verdict suppresses the alert entirely.
    verification_verdict: str | None = None
    #: The subset of risk flags the verifier could not auto-clear — the only
    #: ones a human still needs to check.
    unresolved_flags: tuple[str, ...] = ()

    def render_text(self) -> str:
        other_fees = (
            self.fees.total
            - self.fees.kalshi_fee
            - self.fees.polymarket_fee
            - self.fees.expected_slippage
        )
        lines = [
            f"ARB {self.direction} | {self.kalshi_ticker} <> {self.poly_condition_id[:14]}…",
            f"confidence {self.confidence:.2f} | size {self.size} | {self.depth_summary}",
            (
                f"fees: kalshi ${self.fees.kalshi_fee.to_dollars()} "
                f"poly ${self.fees.polymarket_fee.to_dollars()} "
                f"slippage ${self.fees.expected_slippage.to_dollars()} "
                f"other ${other_fees.to_dollars()}"
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
        if self.verification_verdict is not None:
            lines.append(f"AUTO-VERIFIED: {self.verification_verdict}")
            if self.unresolved_flags:
                lines.append(f"  human must check ({len(self.unresolved_flags)}):")
                lines.extend(f"    - {flag}" for flag in self.unresolved_flags)
        elif self.risk_flags:
            lines.append(f"VERIFY before trading ({len(self.risk_flags)}):")
            lines.extend(f"  - {flag}" for flag in self.risk_flags)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, str | int | float | None | list[str]]:
        return {
            "kalshi_ticker": self.kalshi_ticker,
            "poly_condition_id": self.poly_condition_id,
            "direction": self.direction,
            "confidence": self.confidence,
            "size": self.size,
            "depth_summary": self.depth_summary,
            "net_edge_dollars": str(self.net_edge.to_dollars()),
            "simple_return": str(self.simple_return),
            "annualized_return": str(self.annualized_return),
            "break_even_slippage_per_share": str(self.break_even_slippage_per_share),
            "break_even_extra_fees_dollars": str(self.break_even_extra_fees.to_dollars()),
            "kalshi_fee_dollars": str(self.fees.kalshi_fee.to_dollars()),
            "polymarket_fee_dollars": str(self.fees.polymarket_fee.to_dollars()),
            "bridge_cost_dollars": str(self.fees.bridge_cost.to_dollars()),
            "withdrawal_cost_dollars": str(self.fees.withdrawal_cost.to_dollars()),
            "gas_cost_dollars": str(self.fees.gas_cost.to_dollars()),
            "processor_cost_dollars": str(self.fees.processor_cost.to_dollars()),
            "conversion_cost_dollars": str(self.fees.conversion_cost.to_dollars()),
            "slippage_cost_dollars": str(self.fees.slippage_cost.to_dollars()),
            "unknown_cost_buffer_dollars": str(self.fees.unknown_cost_buffer.to_dollars()),
            "snapshot_id": self.snapshot_id,
            "risk_flags": list(self.risk_flags),
            "verification_verdict": self.verification_verdict,
            "unresolved_flags": list(self.unresolved_flags),
        }


class AlertDeliveryError(Exception):
    """Sanitized alert failure that never includes credential-bearing URLs."""


class AlertSink(Protocol):
    async def send(self, payload: AlertPayload) -> None: ...
