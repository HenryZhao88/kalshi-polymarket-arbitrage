"""Order router — present but DISABLED BY DEFAULT (SPEC prime directive 5).

Every entry point calls clients.geoblock.ensure_execution_allowed first, which
raises ExecutionDisabledError unless ARB_MODE=execution-enabled AND the runtime
geoblock check passes. There is no code path that bypasses the gate.
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
    """Gate first; live order placement is intentionally unimplemented."""
    await ensure_execution_allowed(settings, geoblock)
    raise NotImplementedError(
        "live order routing is not shipped; discovery/alert-only is the supported mode"
    )
