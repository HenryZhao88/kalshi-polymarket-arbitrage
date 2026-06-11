"""Similarity cascade (matching pipeline stage 3):
exact structured match → RapidFuzz score (difflib fallback) → token overlap."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from arb_scanner.app.markets.parsers import parse_features

try:
    from rapidfuzz import fuzz

    def _fuzzy_ratio(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b)) / 100.0

except ImportError:  # pragma: no cover - difflib fallback per SPEC
    from difflib import SequenceMatcher

    def _fuzzy_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


class MatchStage(StrEnum):
    STRUCTURED = "structured"  # strike/direction/time all agree
    FUZZY = "fuzzy"
    TOKEN_OVERLAP = "token_overlap"  # noqa: S105 — not a credential


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    score: float  # [0, 1]
    stage: MatchStage
    structured_conflicts: tuple[str, ...] = field(default=())


def similarity(
    title_a: str,
    title_b: str,
    *,
    determination_time_a: datetime | None = None,
    determination_time_b: datetime | None = None,
) -> SimilarityResult:
    fa, fb = parse_features(title_a), parse_features(title_b)

    conflicts: list[str] = []
    agreements = 0
    comparable = 0
    if fa.strike is not None and fb.strike is not None:
        comparable += 1
        if fa.strike == fb.strike:
            agreements += 1
        else:
            conflicts.append(f"strike {fa.strike} != {fb.strike}")
    if fa.direction and fb.direction:
        comparable += 1
        if fa.direction == fb.direction:
            agreements += 1
        else:
            conflicts.append(f"direction {fa.direction} != {fb.direction}")
    if determination_time_a and determination_time_b:
        comparable += 1
        if determination_time_a == determination_time_b:
            agreements += 1
        else:
            conflicts.append(f"determination_time {determination_time_a} != {determination_time_b}")

    fuzzy = _fuzzy_ratio(fa.normalized_title, fb.normalized_title)
    union = fa.tokens | fb.tokens
    overlap = len(fa.tokens & fb.tokens) / len(union) if union else 0.0

    if comparable >= 2 and agreements == comparable and fuzzy >= 0.5:
        # exact structured agreement on every comparable field
        score = max(fuzzy, 0.7) + 0.3 * (1 - max(fuzzy, 0.7))
        return SimilarityResult(score=score, stage=MatchStage.STRUCTURED)
    if conflicts:
        # hard structured disagreement caps the score regardless of text overlap
        return SimilarityResult(
            score=min(fuzzy, 0.5) * 0.5,
            stage=MatchStage.TOKEN_OVERLAP,
            structured_conflicts=tuple(conflicts),
        )
    if fuzzy >= 0.6:
        return SimilarityResult(score=fuzzy, stage=MatchStage.FUZZY)
    return SimilarityResult(score=max(overlap, fuzzy * 0.8), stage=MatchStage.TOKEN_OVERLAP)
