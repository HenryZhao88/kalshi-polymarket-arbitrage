"""Similarity cascade (matching pipeline stage 3):
exact structured match → RapidFuzz score (difflib fallback) → token overlap."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from arb_scanner.app.markets.parsers import parse_features

# Two genuinely-equivalent markets carry close/end timestamps a few hours apart
# (trading cutoff vs UMA end date). A gap inside this window is a structured
# agreement; a larger gap is simply not comparable here and never a conflict —
# material-horizon rejection belongs to the rule layer
# (rule_equivalence.DETERMINATION_TIME_MAX_DELTA), not the similarity score.
DETERMINATION_TIME_AGREEMENT_WINDOW = timedelta(hours=48)

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
    matched_tokens: tuple[str, ...] = field(default=())


def similarity(
    title_a: str,
    title_b: str,
    *,
    determination_time_a: datetime | None = None,
    determination_time_b: datetime | None = None,
) -> SimilarityResult:
    fa = parse_features(title_a, reference_time=determination_time_a)
    fb = parse_features(title_b, reference_time=determination_time_b)

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
        # A small gap is a structured agreement; a larger gap is left out of the
        # comparable set rather than recorded as a conflict, so it never tanks
        # the score. The rule layer rejects materially different horizons.
        if abs(determination_time_a - determination_time_b) <= DETERMINATION_TIME_AGREEMENT_WINDOW:
            comparable += 1
            agreements += 1
    if fa.event_date is not None and fb.event_date is not None:
        comparable += 1
        if fa.event_date == fb.event_date:
            agreements += 1
        else:
            conflicts.append(f"event_date {fa.event_date} != {fb.event_date}")

    fuzzy = _fuzzy_ratio(fa.normalized_title, fb.normalized_title)
    union = fa.tokens | fb.tokens
    overlap = len(fa.tokens & fb.tokens) / len(union) if union else 0.0
    matched_tokens = tuple(sorted(fa.tokens & fb.tokens))

    if comparable >= 2 and agreements == comparable and fuzzy >= 0.5:
        # exact structured agreement on every comparable field
        score = max(fuzzy, 0.7) + 0.3 * (1 - max(fuzzy, 0.7))
        return SimilarityResult(
            score=score,
            stage=MatchStage.STRUCTURED,
            matched_tokens=matched_tokens,
        )
    if conflicts:
        # hard structured disagreement caps the score regardless of text overlap
        return SimilarityResult(
            score=min(fuzzy, 0.5) * 0.5,
            stage=MatchStage.TOKEN_OVERLAP,
            structured_conflicts=tuple(conflicts),
            matched_tokens=matched_tokens,
        )
    if fuzzy >= 0.6:
        return SimilarityResult(
            score=fuzzy,
            stage=MatchStage.FUZZY,
            matched_tokens=matched_tokens,
        )
    return SimilarityResult(
        score=max(overlap, fuzzy * 0.8),
        stage=MatchStage.TOKEN_OVERLAP,
        matched_tokens=matched_tokens,
    )
