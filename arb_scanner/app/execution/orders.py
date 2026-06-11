"""Order construction — present but DISABLED BY DEFAULT.

Deterministic client order IDs are derived from opportunity facts so retries are
idempotent where venues support them (Kalshi accepts client_order_id).
"""

from __future__ import annotations

import hashlib

from arb_scanner.app.economics import OpportunityEvaluation


def deterministic_client_order_id(
    evaluation: OpportunityEvaluation, *, kalshi_ticker: str, leg: str
) -> str:
    seed = (
        f"{kalshi_ticker}|{leg}|{evaluation.direction}|{evaluation.size}|"
        f"{evaluation.kalshi_leg.vwap}|{evaluation.poly_leg.vwap}"
    )
    return hashlib.sha256(seed.encode()).hexdigest()[:32]
