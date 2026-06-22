"""Two-leg arbitrage executor — DISABLED BY DEFAULT, the single choke point for
live order placement.

Prime directive: there is exactly one place that can send a real order, and it
refuses unless EVERY gate passes. The gates, checked in order and failing
closed:

  1. mode == EXECUTION_ENABLED                  (config)
  2. live_order_placement == True               (explicit second switch)
  3. kill switch is clear                        (risk/kill_switch)
  4. runtime geoblock check passes               (clients/geoblock, async)
  5. each leg's notional <= max_order_notional   (config cap)
  6. balance preflight covers locked capital      (unless disabled)

Even with all gates satisfied, ``execution_dry_run`` (default True) makes the
executor PLAN and log the orders without calling any venue endpoint. Flipping
that to False is the final, deliberate step.

Cross-venue legging risk is the core hazard: if leg 1 fills and leg 2 fails, you
hold a naked position. The executor confirms leg 1's fill before placing leg 2,
and if leg 2 fails it attempts to UNWIND leg 1 (cancel any resting remainder and
sell back the filled portion), surfacing a loud error if the unwind itself
fails. This is best-effort risk reduction, not a guarantee — documented as such.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from arb_scanner.app.clients.geoblock import (
    ExecutionDisabledError,
    GeoblockClient,
    ensure_execution_allowed,
)
from arb_scanner.app.config import Settings
from arb_scanner.app.economics import Direction, OpportunityEvaluation
from arb_scanner.app.execution.adapters import ExecutionClient, OrderResult, OrderStatus
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import Money, Side

log = logging.getLogger("arb_scanner.execution")


class ExecutionOutcome(StrEnum):
    GATED = "gated"  # a gate blocked it; nothing was sent
    PLANNED = "planned"  # dry-run: orders computed and logged, none sent
    FILLED = "filled"  # both legs filled
    LEG1_ONLY_UNWOUND = "leg1_only_unwound"  # leg2 failed, leg1 unwound
    LEG1_ONLY_NAKED = "leg1_only_naked"  # leg2 failed AND unwind failed — danger
    LEG1_FAILED = "leg1_failed"  # leg1 never filled; no exposure taken


@dataclass(frozen=True, slots=True)
class PlannedLeg:
    venue_label: str
    market_id: str
    side: Side
    size: int
    limit_price: Decimal
    notional: Money


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    direction: Direction
    leg1: PlannedLeg
    leg2: PlannedLeg


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    outcome: ExecutionOutcome
    plan: ExecutionPlan | None
    reasons: tuple[str, ...] = ()
    leg1_result: OrderResult | None = None
    leg2_result: OrderResult | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def placed_any_order(self) -> bool:
        return self.leg1_result is not None


def _leg_limit_price(vwap: Decimal, pad: Decimal) -> Decimal:
    """Marketable-limit price: pad the taker VWAP so the limit crosses without
    paying through the whole book. Clamped to (0, 1)."""
    padded = vwap + pad
    if padded <= 0:
        return Decimal("0.01")
    if padded >= 1:
        return Decimal("0.99")
    return padded


def build_plan(
    evaluation: OpportunityEvaluation,
    *,
    kalshi_market_id: str,
    poly_yes_token_id: str,
    poly_no_token_id: str,
    price_pad: Decimal,
) -> ExecutionPlan:
    """Translate an evaluation into the two concrete buy legs.

    Both legs are buys (the economics model buys both sides). Direction picks
    which Kalshi side and which Polymarket token are bought.
    """
    size = evaluation.executable_size
    if evaluation.kalshi_leg.vwap is None or evaluation.poly_leg.vwap is None:
        raise ValueError("cannot build an execution plan for a leg with no fillable depth")
    k_price = _leg_limit_price(evaluation.kalshi_leg.vwap, price_pad)
    p_price = _leg_limit_price(evaluation.poly_leg.vwap, price_pad)
    if evaluation.direction is Direction.KALSHI_YES_POLY_NO:
        kalshi_side, poly_token = Side.YES, poly_no_token_id
    else:
        kalshi_side, poly_token = Side.NO, poly_yes_token_id

    leg1 = PlannedLeg(
        venue_label="kalshi",
        market_id=kalshi_market_id,
        side=kalshi_side,
        size=size,
        limit_price=k_price,
        notional=Money.from_dollars((k_price * size).quantize(Decimal("0.0001"))),
    )
    leg2 = PlannedLeg(
        venue_label="polymarket",
        market_id=poly_token,
        side=Side.NO if evaluation.direction is Direction.KALSHI_YES_POLY_NO else Side.YES,
        size=size,
        limit_price=p_price,
        notional=Money.from_dollars((p_price * size).quantize(Decimal("0.0001"))),
    )
    return ExecutionPlan(direction=evaluation.direction, leg1=leg1, leg2=leg2)


def _gate_reasons(settings: Settings, kill_switch: KillSwitch, plan: ExecutionPlan) -> list[str]:
    """Synchronous gates (mode, second switch, kill switch, size caps)."""
    reasons: list[str] = []
    if settings.mode.value != "execution-enabled":
        reasons.append("mode is not execution-enabled")
    if not settings.live_order_placement:
        reasons.append("live_order_placement gate is off")
    if kill_switch.engaged:
        reasons.append("kill switch engaged")
    cap = Money.from_dollars(settings.max_order_notional_dollars)
    for leg in (plan.leg1, plan.leg2):
        if leg.notional > cap:
            reasons.append(
                f"{leg.venue_label} leg notional {leg.notional.to_dollars()} > cap "
                f"{settings.max_order_notional_dollars}"
            )
    return reasons


class TwoLegExecutor:
    """Drives a single opportunity through the gated two-leg flow."""

    def __init__(
        self,
        *,
        settings: Settings,
        kalshi: ExecutionClient,
        polymarket: ExecutionClient,
        geoblock: GeoblockClient,
        kill_switch: KillSwitch,
    ) -> None:
        self._settings = settings
        self._kalshi = kalshi
        self._polymarket = polymarket
        self._geoblock = geoblock
        self._kill = kill_switch

    def _client_for(self, leg: PlannedLeg) -> ExecutionClient:
        return self._kalshi if leg.venue_label == "kalshi" else self._polymarket

    async def execute(self, plan: ExecutionPlan) -> ExecutionRecord:
        # Gates 1,2,3,5 (synchronous), then 4 (async geoblock), then 6 (balance).
        reasons = _gate_reasons(self._settings, self._kill, plan)
        if reasons:
            log.warning("execution gated: %s", "; ".join(reasons))
            return ExecutionRecord(ExecutionOutcome.GATED, plan, tuple(reasons))

        try:
            await ensure_execution_allowed(self._settings, self._geoblock)
        except ExecutionDisabledError as exc:
            log.warning("execution gated by geoblock/mode: %s", exc)
            return ExecutionRecord(ExecutionOutcome.GATED, plan, (str(exc),))

        if self._settings.require_balance_preflight:
            shortfall = await self._balance_shortfall(plan)
            if shortfall:
                log.warning("execution gated: %s", "; ".join(shortfall))
                return ExecutionRecord(ExecutionOutcome.GATED, plan, tuple(shortfall))

        if self._settings.execution_dry_run:
            log.info(
                "DRY-RUN execution plan (no orders sent): leg1 %s %s x%d @ %s | "
                "leg2 %s %s x%d @ %s",
                plan.leg1.venue_label,
                plan.leg1.side.value,
                plan.leg1.size,
                plan.leg1.limit_price,
                plan.leg2.venue_label,
                plan.leg2.side.value,
                plan.leg2.size,
                plan.leg2.limit_price,
            )
            return ExecutionRecord(ExecutionOutcome.PLANNED, plan)

        return await self._place_both_legs(plan)

    async def _balance_shortfall(self, plan: ExecutionPlan) -> list[str]:
        reasons: list[str] = []
        for leg in (plan.leg1, plan.leg2):
            client = self._client_for(leg)
            try:
                available = await client.available_balance()
            except Exception as exc:
                reasons.append(f"{leg.venue_label} balance check failed: {type(exc).__name__}")
                continue
            if available < leg.notional:
                reasons.append(
                    f"{leg.venue_label} balance {available.to_dollars()} < required "
                    f"{leg.notional.to_dollars()}"
                )
        return reasons

    async def _place_both_legs(self, plan: ExecutionPlan) -> ExecutionRecord:
        leg1_client = self._client_for(plan.leg1)
        leg2_client = self._client_for(plan.leg2)

        # Leg 1.
        try:
            leg1 = await leg1_client.place_buy(
                market_id=plan.leg1.market_id,
                side=plan.leg1.side,
                size=plan.leg1.size,
                limit_price=plan.leg1.limit_price,
            )
        except Exception as exc:
            log.exception("leg1 placement failed; no exposure taken")
            return ExecutionRecord(
                ExecutionOutcome.LEG1_FAILED, plan, (f"leg1 error: {type(exc).__name__}",)
            )
        if not leg1.fully_filled:
            # Leg 1 not (fully) filled — do NOT open leg 2. Cancel any remainder.
            note = await self._safe_cancel(leg1_client, leg1)
            return ExecutionRecord(
                ExecutionOutcome.LEG1_FAILED,
                plan,
                (f"leg1 not fully filled (status={leg1.status.value})",),
                leg1_result=leg1,
                notes=note,
            )

        # Leg 2 — confirmed leg 1 filled, so now we are exposed until leg 2 lands.
        try:
            leg2 = await leg2_client.place_buy(
                market_id=plan.leg2.market_id,
                side=plan.leg2.side,
                size=plan.leg2.size,
                limit_price=plan.leg2.limit_price,
            )
        except Exception as exc:
            log.exception("leg2 placement raised; attempting leg1 unwind")
            return await self._unwind(plan, leg1, leg2=None, error=type(exc).__name__)

        if leg2.fully_filled:
            log.info("two-leg execution complete: both legs filled")
            return ExecutionRecord(
                ExecutionOutcome.FILLED, plan, leg1_result=leg1, leg2_result=leg2
            )

        log.warning("leg2 not fully filled (status=%s); attempting unwind", leg2.status.value)
        return await self._unwind(plan, leg1, leg2=leg2, error=f"status={leg2.status.value}")

    async def _unwind(
        self,
        plan: ExecutionPlan,
        leg1: OrderResult,
        *,
        leg2: OrderResult | None,
        error: str,
    ) -> ExecutionRecord:
        """Best-effort unwind of the filled leg 1 after a leg-2 failure."""
        leg1_client = self._client_for(plan.leg1)
        notes: list[str] = [f"leg2 failed: {error}"]
        # Cancel any leg-2 remainder first (frees a partial resting order).
        if leg2 is not None and leg2.order_id and leg2.status is OrderStatus.PARTIAL:
            notes += await self._safe_cancel(self._client_for(plan.leg2), leg2)
        # Sell back the filled leg-1 quantity.
        try:
            unwind = await leg1_client.place_sell(
                market_id=plan.leg1.market_id,
                side=plan.leg1.side,
                size=leg1.filled_size,
                limit_price=Decimal("0.01"),  # aggressive: exit, don't price-hunt
            )
        except Exception as exc:
            notes.append(f"UNWIND FAILED: {type(exc).__name__} — NAKED leg1 position")
            log.error("UNWIND FAILED — naked leg1 position on %s", plan.leg1.market_id)
            return ExecutionRecord(
                ExecutionOutcome.LEG1_ONLY_NAKED,
                plan,
                (f"leg2 failed: {error}", "unwind failed"),
                leg1_result=leg1,
                leg2_result=leg2,
                notes=tuple(notes),
            )
        if unwind.any_fill:
            notes.append(f"leg1 unwound ({unwind.filled_size} sold)")
            return ExecutionRecord(
                ExecutionOutcome.LEG1_ONLY_UNWOUND,
                plan,
                (f"leg2 failed: {error}",),
                leg1_result=leg1,
                leg2_result=leg2,
                notes=tuple(notes),
            )
        notes.append("UNWIND order did not fill — NAKED leg1 position")
        log.error("UNWIND did not fill — naked leg1 position on %s", plan.leg1.market_id)
        return ExecutionRecord(
            ExecutionOutcome.LEG1_ONLY_NAKED,
            plan,
            (f"leg2 failed: {error}", "unwind unfilled"),
            leg1_result=leg1,
            leg2_result=leg2,
            notes=tuple(notes),
        )

    async def _safe_cancel(self, client: ExecutionClient, order: OrderResult) -> tuple[str, ...]:
        if not order.order_id:
            return ()
        try:
            await client.cancel(order.order_id)
        except Exception as exc:
            log.warning("cancel of %s failed: %s", order.order_id, type(exc).__name__)
            return (f"cancel {order.order_id} failed: {type(exc).__name__}",)
        return (f"canceled {order.order_id}",)
