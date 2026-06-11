"""Cross-venue discovery: turn raw venue payloads into scored MatchedPairs.

Kalshi MVE (multivariate) series are excluded — they have empty REST books
(docs/VERIFICATION.md §1.4 / flag 7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from arb_scanner.app.markets.matching import similarity
from arb_scanner.app.markets.rule_equivalence import (
    KalshiRuleFacts,
    MatchStatus,
    PolymarketRuleFacts,
    RuleEquivalenceResult,
    decide_status,
    validate_rules,
)


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """Pipeline output, persisted in full (SPEC Phase 3)."""

    kalshi_ticker: str
    poly_condition_id: str
    poly_yes_token_id: str
    poly_no_token_id: str
    confidence: float
    status: MatchStatus
    matched_fields: dict[str, str] = field(default_factory=dict)
    differing_fields: dict[str, str] = field(default_factory=dict)
    rule_warnings: tuple[str, ...] = ()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def kalshi_is_scannable(market: dict[str, Any]) -> bool:
    return "MVE" not in market.get("ticker", "") and market.get("status") == "active"


def poly_token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
    """(yes_token, no_token) from a Gamma market payload, or None."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            token_list = json.loads(raw)
        except ValueError:
            return None
    else:
        token_list = raw
    if not token_list or len(token_list) < 2:
        return None
    return str(token_list[0]), str(token_list[1])


def evaluate_pair(kalshi_market: dict[str, Any], poly_market: dict[str, Any]) -> MatchedPair | None:
    """Run stages 1–5 for one candidate pair. Returns None if unmatchable."""
    tokens = poly_token_ids(poly_market)
    if tokens is None:
        return None

    kalshi_time = _parse_time(
        kalshi_market.get("expected_expiration_time") or kalshi_market.get("close_time")
    )
    poly_time = _parse_time(poly_market.get("endDate"))

    sim = similarity(
        kalshi_market.get("title", ""),
        poly_market.get("question", ""),
        determination_time_a=kalshi_time,
        determination_time_b=poly_time,
    )

    rules: RuleEquivalenceResult = validate_rules(
        KalshiRuleFacts(
            determination_time=kalshi_time,
            resolution_source=str(kalshi_market.get("rules_primary") or "")[:200],
            can_close_early=bool(kalshi_market.get("can_close_early")),
            is_sports=str(kalshi_market.get("category", "")).lower() == "sports",
            void_policy="none",
        ),
        PolymarketRuleFacts(
            determination_time=poly_time,
            resolution_source=str(poly_market.get("resolutionSource") or "")[:200],
            uma_resolution=True,  # Polymarket resolution is UMA-based
            is_sports="sports" in [t.lower() for t in poly_market.get("tags") or []],
            game_start_time=_parse_time(poly_market.get("gameStartTime")),
            void_policy="none",
        ),
    )
    status = decide_status(sim.score, rules)

    matched: dict[str, str] = {"similarity_stage": sim.stage.value}
    differing: dict[str, str] = {f"conflict_{i}": c for i, c in enumerate(sim.structured_conflicts)}
    for i, failure in enumerate(rules.hard_failures):
        differing[f"rule_{i}"] = failure

    return MatchedPair(
        kalshi_ticker=kalshi_market["ticker"],
        poly_condition_id=poly_market.get("conditionId", ""),
        poly_yes_token_id=tokens[0],
        poly_no_token_id=tokens[1],
        confidence=round(sim.score, 4),
        status=status,
        matched_fields=matched,
        differing_fields=differing,
        rule_warnings=rules.warnings,
    )
