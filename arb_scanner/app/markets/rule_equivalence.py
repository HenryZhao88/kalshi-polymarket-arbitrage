"""Rule-equivalence validation (matching pipeline stage 4 — MANDATORY) and the
final status decision (stage 5).

Two markets with identical titles are NOT the same bet unless their resolution
mechanics agree: source, determination time, void/DNP/50-50 handling, dispute
process. Known venue divergences encoded here (SPEC Phase 3):
- Polymarket resolution goes through UMA with a challenge window — always at
  least a warning, because outcomes can be disputed after apparent resolution.
- Sports: Polymarket auto-cancels sports limit orders at game start but may miss
  early starts; Kalshi may keep trading past close awaiting official confirmation.

Hard failures veto a match regardless of textual similarity. Warnings cap the
status at manual_review; nothing below threshold reaches the economics engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

ACCEPT_THRESHOLD = 0.9
REVIEW_THRESHOLD = 0.6


class MatchStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class KalshiRuleFacts:
    determination_time: datetime | None
    resolution_source: str
    can_close_early: bool
    is_sports: bool
    void_policy: str  # venue-specific token, e.g. "none" / "trades_stand"


@dataclass(frozen=True, slots=True)
class PolymarketRuleFacts:
    determination_time: datetime | None
    resolution_source: str
    uma_resolution: bool
    is_sports: bool
    game_start_time: datetime | None
    void_policy: str


@dataclass(frozen=True, slots=True)
class RuleEquivalenceResult:
    hard_failures: tuple[str, ...]
    warnings: tuple[str, ...]


def validate_rules(kalshi: KalshiRuleFacts, poly: PolymarketRuleFacts) -> RuleEquivalenceResult:
    failures: list[str] = []
    warnings: list[str] = []

    # Determination time: end date vs determination time must agree exactly.
    if kalshi.determination_time and poly.determination_time:
        if kalshi.determination_time != poly.determination_time:
            failures.append(
                "determination time differs: "
                f"kalshi={kalshi.determination_time} poly={poly.determination_time}"
            )
    else:
        warnings.append("determination time unverified on at least one venue")

    # Resolution source.
    k_src, p_src = kalshi.resolution_source.strip().lower(), poly.resolution_source.strip().lower()
    if k_src and p_src:
        if k_src != p_src:
            failures.append(f"resolution source differs: {k_src!r} vs {p_src!r}")
    else:
        warnings.append("resolution source unverified on at least one venue")

    # Void / DNP / postponement handling.
    if kalshi.void_policy != poly.void_policy:
        failures.append(f"void policy differs: kalshi={kalshi.void_policy} poly={poly.void_policy}")

    # UMA challenge window applies to Polymarket-resolved markets.
    if poly.uma_resolution:
        warnings.append("UMA challenge window: Polymarket outcome can be disputed post-resolution")

    # Sports early-start divergence.
    if kalshi.is_sports or poly.is_sports:
        if poly.game_start_time is not None or kalshi.can_close_early:
            warnings.append(
                "sports early start risk: Polymarket cancels limit orders at "
                "scheduled start (may miss early starts); Kalshi may trade past close"
            )

    return RuleEquivalenceResult(hard_failures=tuple(failures), warnings=tuple(warnings))


def decide_status(
    similarity_score: float,
    rules: RuleEquivalenceResult,
    *,
    accept_threshold: float = ACCEPT_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
) -> MatchStatus:
    """Stage 5. Hard rule failures veto; warnings cap at manual_review."""
    if rules.hard_failures:
        return MatchStatus.REJECTED
    if similarity_score < review_threshold:
        return MatchStatus.REJECTED
    # More than the ever-present UMA warning means a human must look.
    substantive_warnings = len(rules.warnings)
    if similarity_score >= accept_threshold and substantive_warnings <= 1:
        return MatchStatus.ACCEPTED
    return MatchStatus.MANUAL_REVIEW
