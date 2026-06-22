"""Execution runner tests (no network): gate-status snapshot and the
opportunity-routing loop over a fake executor."""

from decimal import Decimal
from typing import Any

from arb_scanner.app.config import Mode, Settings
from arb_scanner.app.execution.executor import (
    ExecutionOutcome,
    ExecutionRecord,
)
from arb_scanner.app.execution.runner import (
    execute_alertable_opportunities,
    gate_status,
)
from arb_scanner.app.risk.kill_switch import KillSwitch


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None}
    base.update(over)
    return Settings(**base)


class TestGateStatus:
    def test_discovery_default_cannot_attempt_live(self) -> None:
        status = gate_status(_settings(), KillSwitch(), kalshi_signer=None)
        assert status.mode_ok is False
        assert status.second_switch_ok is False
        assert status.dry_run is True
        assert status.can_attempt_live is False
        assert any("dry-run/blocked" in line for line in status.render_lines())

    def test_all_standing_gates_open_can_attempt(self) -> None:
        settings = _settings(
            mode=Mode.EXECUTION_ENABLED,
            live_order_placement=True,
            execution_dry_run=False,
        )
        status = gate_status(settings, KillSwitch(), kalshi_signer=None)
        assert status.can_attempt_live is True
        assert any("WOULD ATTEMPT LIVE ORDERS" in line for line in status.render_lines())

    def test_kill_switch_blocks_attempt(self) -> None:
        settings = _settings(
            mode=Mode.EXECUTION_ENABLED,
            live_order_placement=True,
            execution_dry_run=False,
        )
        kill = KillSwitch()
        kill.engage()
        status = gate_status(settings, kill, kalshi_signer=None)
        assert status.can_attempt_live is False


class FakePair:
    def __init__(self) -> None:
        self.kalshi_ticker = "KXBTC-T70000"
        self.poly_condition_id = "0xcond"
        self.poly_yes_token_id = "0xyes"
        self.poly_no_token_id = "0xno"


class FakeEval:
    from arb_scanner.app.economics import Direction as _D

    direction = _D.KALSHI_YES_POLY_NO
    executable_size = 10

    from arb_scanner.app.books.depth import DepthResult as _DR

    kalshi_leg = _DR(
        requested=Decimal(10), fillable=Decimal(10), vwap=Decimal("0.40"), levels_consumed=1
    )
    poly_leg = _DR(
        requested=Decimal(10), fillable=Decimal(10), vwap=Decimal("0.58"), levels_consumed=1
    )


class FakeReport:
    def __init__(self, opportunities: list[Any]) -> None:
        self.opportunities = opportunities


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, plan: Any) -> ExecutionRecord:
        self.calls += 1
        return ExecutionRecord(ExecutionOutcome.PLANNED, plan)


class TestRoutingLoop:
    async def test_only_alertable_opportunities_are_routed(self) -> None:
        report = FakeReport(
            [
                (FakePair(), FakeEval(), []),  # alertable
                (FakePair(), FakeEval(), ["some rejection reason"]),  # not alertable
            ]
        )
        executor = FakeExecutor()
        records = await execute_alertable_opportunities(
            executor,  # type: ignore[arg-type]
            report,  # type: ignore[arg-type]
            price_pad=Decimal("0.01"),
        )
        assert executor.calls == 1
        assert len(records) == 1
        assert records[0].outcome is ExecutionOutcome.PLANNED

    async def test_max_executions_caps_routing(self) -> None:
        report = FakeReport([(FakePair(), FakeEval(), []) for _ in range(5)])
        executor = FakeExecutor()
        records = await execute_alertable_opportunities(
            executor,  # type: ignore[arg-type]
            report,  # type: ignore[arg-type]
            price_pad=Decimal("0.01"),
            max_executions=2,
        )
        assert len(records) == 2
