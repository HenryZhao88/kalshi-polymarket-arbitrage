"""Two-leg executor tests (no network): gates, dry-run, fill, and unwind paths.

The executor is the single live-order choke point, so these tests pin the safety
behavior: every gate fails closed, dry-run sends nothing, and a leg-2 failure
unwinds leg 1 (or loudly reports a naked position when the unwind itself fails).
"""

from decimal import Decimal
from typing import Any

import pytest

from arb_scanner.app.config import Mode, Settings
from arb_scanner.app.economics import Direction
from arb_scanner.app.execution.adapters import OrderResult, OrderStatus
from arb_scanner.app.execution.executor import (
    ExecutionOutcome,
    ExecutionPlan,
    PlannedLeg,
    TwoLegExecutor,
    build_plan,
)
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import Money, Side, Venue


def live_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "mode": Mode.EXECUTION_ENABLED,
        "live_order_placement": True,
        "execution_dry_run": False,
        "require_balance_preflight": False,
        "max_order_notional_dollars": Decimal("1000"),
    }
    base.update(over)
    return Settings(**base)


class FakeGeoblock:
    def __init__(self, blocked: bool = False) -> None:
        self._blocked = blocked

    async def check(self) -> Any:
        from arb_scanner.app.clients.geoblock import GeoblockStatus

        return GeoblockStatus(blocked=self._blocked, country="US", region="NY")


def _result(
    venue: Venue, status: OrderStatus, *, requested: int, filled: int, order_id: str | None = "o"
) -> OrderResult:
    return OrderResult(
        venue=venue,
        order_id=order_id,
        status=status,
        requested_size=requested,
        filled_size=filled,
        avg_price=Decimal("0.4"),
        raw={},
    )


class ScriptedClient:
    """An ExecutionClient whose buy/sell results are scripted per call."""

    def __init__(
        self,
        venue: Venue,
        *,
        balance: Money | None = None,
        buy: OrderResult | Exception | None = None,
        sell: OrderResult | Exception | None = None,
    ) -> None:
        self.venue = venue
        self._balance = balance if balance is not None else Money.from_dollars("10000")
        self._buy = buy
        self._sell = sell
        self.buys: list[dict[str, Any]] = []
        self.sells: list[dict[str, Any]] = []
        self.cancels: list[str] = []

    async def available_balance(self) -> Money:
        return self._balance

    async def place_buy(self, **kwargs: Any) -> OrderResult:
        self.buys.append(kwargs)
        if isinstance(self._buy, Exception):
            raise self._buy
        assert self._buy is not None
        return self._buy

    async def place_sell(self, **kwargs: Any) -> OrderResult:
        self.sells.append(kwargs)
        if isinstance(self._sell, Exception):
            raise self._sell
        assert self._sell is not None
        return self._sell

    async def cancel(self, order_id: str) -> None:
        self.cancels.append(order_id)


def make_plan(notional_dollars: str = "40") -> ExecutionPlan:
    leg1 = PlannedLeg(
        venue_label="kalshi",
        market_id="KXBTC-T70000",
        side=Side.YES,
        size=100,
        limit_price=Decimal("0.40"),
        notional=Money.from_dollars(notional_dollars),
    )
    leg2 = PlannedLeg(
        venue_label="polymarket",
        market_id="0xtoken",
        side=Side.NO,
        size=100,
        limit_price=Decimal("0.58"),
        notional=Money.from_dollars(notional_dollars),
    )
    return ExecutionPlan(direction=Direction.KALSHI_YES_POLY_NO, leg1=leg1, leg2=leg2)


def make_executor(
    settings: Settings,
    *,
    kalshi: ScriptedClient,
    poly: ScriptedClient,
    kill: KillSwitch | None = None,
    geoblock: FakeGeoblock | None = None,
) -> TwoLegExecutor:
    return TwoLegExecutor(
        settings=settings,
        kalshi=kalshi,  # type: ignore[arg-type]
        polymarket=poly,  # type: ignore[arg-type]
        geoblock=geoblock or FakeGeoblock(),  # type: ignore[arg-type]
        kill_switch=kill or KillSwitch(),
    )


class TestGates:
    async def test_discovery_mode_blocks_and_sends_nothing(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        settings = live_settings(mode=Mode.DISCOVERY_ONLY)
        record = await make_executor(settings, kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.GATED
        assert any("execution-enabled" in r for r in record.reasons)
        assert not kalshi.buys and not poly.buys

    async def test_second_switch_off_blocks(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        settings = live_settings(live_order_placement=False)
        record = await make_executor(settings, kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.GATED
        assert any("live_order_placement" in r for r in record.reasons)
        assert not kalshi.buys

    async def test_kill_switch_blocks(self) -> None:
        kill = KillSwitch()
        kill.engage()
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly, kill=kill).execute(
            make_plan()
        )
        assert record.outcome is ExecutionOutcome.GATED
        assert any("kill switch" in r for r in record.reasons)

    async def test_notional_cap_blocks(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        settings = live_settings(max_order_notional_dollars=Decimal("10"))
        record = await make_executor(settings, kalshi=kalshi, poly=poly).execute(make_plan("40"))
        assert record.outcome is ExecutionOutcome.GATED
        assert any("cap" in r for r in record.reasons)
        assert not kalshi.buys

    async def test_geoblock_blocks(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        record = await make_executor(
            live_settings(), kalshi=kalshi, poly=poly, geoblock=FakeGeoblock(blocked=True)
        ).execute(make_plan())
        assert record.outcome is ExecutionOutcome.GATED
        assert not kalshi.buys

    async def test_balance_preflight_blocks_when_short(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI, balance=Money.from_dollars("5"))
        poly = ScriptedClient(Venue.POLYMARKET, balance=Money.from_dollars("5"))
        settings = live_settings(require_balance_preflight=True)
        record = await make_executor(settings, kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.GATED
        assert any("balance" in r for r in record.reasons)
        assert not kalshi.buys


class TestDryRun:
    async def test_dry_run_plans_but_sends_nothing(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI)
        poly = ScriptedClient(Venue.POLYMARKET)
        settings = live_settings(execution_dry_run=True)
        record = await make_executor(settings, kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.PLANNED
        assert not kalshi.buys and not poly.buys


class TestLiveFlow:
    async def test_both_legs_fill(self) -> None:
        kalshi = ScriptedClient(
            Venue.KALSHI, buy=_result(Venue.KALSHI, OrderStatus.FILLED, requested=100, filled=100)
        )
        poly = ScriptedClient(
            Venue.POLYMARKET,
            buy=_result(Venue.POLYMARKET, OrderStatus.FILLED, requested=100, filled=100),
        )
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.FILLED
        assert len(kalshi.buys) == 1 and len(poly.buys) == 1
        assert not kalshi.sells  # no unwind needed

    async def test_leg1_not_filled_skips_leg2(self) -> None:
        kalshi = ScriptedClient(
            Venue.KALSHI,
            buy=_result(Venue.KALSHI, OrderStatus.RESTING, requested=100, filled=0),
        )
        poly = ScriptedClient(Venue.POLYMARKET)
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.LEG1_FAILED
        assert not poly.buys  # leg2 never attempted
        assert kalshi.cancels == ["o"]  # remainder canceled

    async def test_leg2_failure_unwinds_leg1(self) -> None:
        kalshi = ScriptedClient(
            Venue.KALSHI,
            buy=_result(Venue.KALSHI, OrderStatus.FILLED, requested=100, filled=100),
            sell=_result(Venue.KALSHI, OrderStatus.FILLED, requested=100, filled=100),
        )
        poly = ScriptedClient(Venue.POLYMARKET, buy=RuntimeError("clob 503"))
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.LEG1_ONLY_UNWOUND
        assert len(kalshi.sells) == 1  # leg1 sold back
        assert kalshi.sells[0]["size"] == 100

    async def test_leg2_failure_and_unwind_failure_is_naked(self) -> None:
        kalshi = ScriptedClient(
            Venue.KALSHI,
            buy=_result(Venue.KALSHI, OrderStatus.FILLED, requested=100, filled=100),
            sell=RuntimeError("kalshi down"),
        )
        poly = ScriptedClient(Venue.POLYMARKET, buy=RuntimeError("clob 503"))
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.LEG1_ONLY_NAKED
        assert any("NAKED" in note for note in record.notes)

    async def test_leg1_placement_exception_takes_no_exposure(self) -> None:
        kalshi = ScriptedClient(Venue.KALSHI, buy=RuntimeError("kalshi 500"))
        poly = ScriptedClient(Venue.POLYMARKET)
        record = await make_executor(live_settings(), kalshi=kalshi, poly=poly).execute(make_plan())
        assert record.outcome is ExecutionOutcome.LEG1_FAILED
        assert not poly.buys


class TestBuildPlan:
    def test_direction_selects_sides_and_tokens(self) -> None:
        from arb_scanner.app.books.depth import DepthResult

        class Eval:
            direction = Direction.KALSHI_YES_POLY_NO
            executable_size = 50
            kalshi_leg = DepthResult(
                requested=Decimal(50), fillable=Decimal(50), vwap=Decimal("0.40"), levels_consumed=1
            )
            poly_leg = DepthResult(
                requested=Decimal(50), fillable=Decimal(50), vwap=Decimal("0.58"), levels_consumed=1
            )

        plan = build_plan(
            Eval(),  # type: ignore[arg-type]
            kalshi_market_id="KXBTC-T70000",
            poly_yes_token_id="0xyes",
            poly_no_token_id="0xno",
            price_pad=Decimal("0.01"),
        )
        assert plan.leg1.side is Side.YES
        assert plan.leg1.market_id == "KXBTC-T70000"
        assert plan.leg1.limit_price == Decimal("0.41")  # vwap + pad
        assert plan.leg2.market_id == "0xno"  # YES/NO -> buy poly NO token
        assert plan.leg2.side is Side.NO

    def test_zero_depth_leg_raises(self) -> None:
        from arb_scanner.app.books.depth import DepthResult

        class Eval:
            direction = Direction.KALSHI_YES_POLY_NO
            executable_size = 0
            kalshi_leg = DepthResult(
                requested=Decimal(0), fillable=Decimal(0), vwap=None, levels_consumed=0
            )
            poly_leg = DepthResult(
                requested=Decimal(0), fillable=Decimal(0), vwap=None, levels_consumed=0
            )

        with pytest.raises(ValueError, match="fillable depth"):
            build_plan(
                Eval(),  # type: ignore[arg-type]
                kalshi_market_id="K",
                poly_yes_token_id="y",
                poly_no_token_id="n",
                price_pad=Decimal("0.01"),
            )
