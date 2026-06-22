"""Legacy single-order router shim — SUPERSEDED by execution/executor.py.

The supported live path is the gated two-leg ``TwoLegExecutor`` (see
``execution/executor.py`` and ``execution/runner.py``), which legs both venues
atomically with unwind-on-failure. This single-order entry point remains only so
older imports resolve; it still gates first (geoblock + mode) and otherwise
declines, directing callers to the executor.
"""

from __future__ import annotations

from arb_scanner.app.clients.geoblock import GeoblockClient, ensure_execution_allowed
from arb_scanner.app.config import Settings
from arb_scanner.app.economics import OpportunityEvaluation


async def route_order(
    settings: Settings,
    geoblock: GeoblockClient,
    evaluation: OpportunityEvaluation,
) -> None:
    """Gate first; single-leg routing is not the supported path."""
    await ensure_execution_allowed(settings, geoblock)
    raise NotImplementedError(
        "single-order routing is superseded; use execution.executor.TwoLegExecutor "
        "(wired via execution.runner / `arb-scanner execute`)"
    )
