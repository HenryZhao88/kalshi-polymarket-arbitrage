"""Execution runner: assemble the gated executor stack and drive a scan's
alertable opportunities through it.

This is the wiring between discovery and the executor. It stays fail-closed:
without execution mode + the second switch it prints the gate status and runs
nothing live (the executor itself still re-checks every gate). The Polymarket
signing client is only constructed when the optional dependency is present and a
private key is configured; otherwise its leg fails closed at call time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiohttp

from arb_scanner.app.clients.geoblock import GeoblockClient
from arb_scanner.app.clients.kalshi_rest import KalshiRestClient, KalshiSigner
from arb_scanner.app.config import Mode, Settings
from arb_scanner.app.execution.adapters import (
    KalshiExecutionAdapter,
    PolymarketExecutionAdapter,
)
from arb_scanner.app.execution.executor import (
    ExecutionRecord,
    TwoLegExecutor,
    build_plan,
)
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.scanner import ScanReport

log = logging.getLogger("arb_scanner.execution.runner")


def kalshi_signer_from_settings(settings: Settings) -> KalshiSigner | None:
    """Build a Kalshi signer from configured key id + PEM path, or None."""
    if settings.kalshi_api_key_id is None or settings.kalshi_private_key_path is None:
        return None
    pem = Path(settings.kalshi_private_key_path).read_bytes()
    return KalshiSigner(settings.kalshi_api_key_id.get_secret_value(), pem)


@dataclass(frozen=True, slots=True)
class GateStatus:
    """Human-readable snapshot of the standing (non-network) execution gates."""

    mode_ok: bool
    second_switch_ok: bool
    kill_switch_clear: bool
    dry_run: bool
    kalshi_signer_present: bool
    polymarket_signing_available: bool

    @property
    def can_attempt_live(self) -> bool:
        """All standing gates clear AND not in dry-run. Geoblock + balance are
        still checked at call time by the executor."""
        return (
            self.mode_ok and self.second_switch_ok and self.kill_switch_clear and not self.dry_run
        )

    def render_lines(self) -> list[str]:
        def mark(ok: bool) -> str:
            return "OK" if ok else "BLOCKED"

        dry = "ON (no live orders)" if self.dry_run else "off"
        return [
            "execution gate status:",
            f"  mode=execution-enabled ............. {mark(self.mode_ok)}",
            f"  live_order_placement=true .......... {mark(self.second_switch_ok)}",
            f"  kill switch clear .................. {mark(self.kill_switch_clear)}",
            f"  execution_dry_run .................. {dry}",
            f"  kalshi signer configured .......... {mark(self.kalshi_signer_present)}",
            f"  polymarket signing available ...... {mark(self.polymarket_signing_available)}",
            (
                "  -> WOULD ATTEMPT LIVE ORDERS (geoblock + balance still checked)"
                if self.can_attempt_live
                else "  -> dry-run/blocked: no live orders this pass"
            ),
        ]


def gate_status(
    settings: Settings, kill_switch: KillSwitch, *, kalshi_signer: KalshiSigner | None
) -> GateStatus:
    return GateStatus(
        mode_ok=settings.mode is Mode.EXECUTION_ENABLED,
        second_switch_ok=settings.live_order_placement,
        kill_switch_clear=not kill_switch.engaged,
        dry_run=settings.execution_dry_run,
        kalshi_signer_present=kalshi_signer is not None,
        polymarket_signing_available=PolymarketExecutionAdapter.is_available(),
    )


def build_executor(
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    kill_switch: KillSwitch,
    kalshi_signer: KalshiSigner | None,
    polymarket_signing_client: object | None = None,
) -> TwoLegExecutor:
    kalshi_rest = KalshiRestClient(session, signer=kalshi_signer)
    return TwoLegExecutor(
        settings=settings,
        kalshi=KalshiExecutionAdapter(kalshi_rest),
        polymarket=PolymarketExecutionAdapter(signing_client=polymarket_signing_client),
        geoblock=GeoblockClient(session),
        kill_switch=kill_switch,
    )


async def execute_alertable_opportunities(
    executor: TwoLegExecutor,
    report: ScanReport,
    *,
    price_pad: Decimal,
    max_executions: int | None = None,
) -> list[ExecutionRecord]:
    """Drive each alertable (no-reason) opportunity through the gated executor."""
    records: list[ExecutionRecord] = []
    for pair, evaluation, reasons in report.opportunities:
        if reasons:
            continue
        if max_executions is not None and len(records) >= max_executions:
            break
        plan = build_plan(
            evaluation,
            kalshi_market_id=pair.kalshi_ticker,
            poly_yes_token_id=pair.poly_yes_token_id,
            poly_no_token_id=pair.poly_no_token_id,
            price_pad=price_pad,
        )
        records.append(await executor.execute(plan))
    return records
