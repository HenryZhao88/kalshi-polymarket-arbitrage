"""Manual-review ranking tests; every row remains unsafe."""

from dataclasses import replace

from arb_scanner.app.markets.discovery import (
    ManualReviewSort,
    MatchedPair,
    sort_manual_review_pairs,
)
from arb_scanner.app.markets.rule_equivalence import MatchStatus

BASE = MatchedPair(
    kalshi_ticker="K1",
    kalshi_title="Example market",
    poly_condition_id="0x1",
    poly_question="Example market",
    poly_yes_token_id="yes",
    poly_no_token_id="no",
    confidence=0.8,
    status=MatchStatus.MANUAL_REVIEW,
)


def test_missing_fields_sort_prefers_more_complete_evidence() -> None:
    incomplete = replace(
        BASE,
        poly_condition_id="incomplete",
        confidence=0.95,
        missing_rule_fields=("source", "void", "time"),
    )
    complete = replace(
        BASE,
        poly_condition_id="complete",
        missing_rule_fields=("void",),
    )
    rows = sort_manual_review_pairs([incomplete, complete], ManualReviewSort.MISSING_FIELDS)
    assert rows[0].poly_condition_id == "complete"
    assert all(row.status is MatchStatus.MANUAL_REVIEW for row in rows)


def test_hypothetical_edge_sort_places_uncomputed_last() -> None:
    computed = replace(
        BASE,
        poly_condition_id="computed",
        hypothetical_economics={"net_edge": "2.5"},
    )
    rows = sort_manual_review_pairs([BASE, computed], ManualReviewSort.HYPOTHETICAL_EDGE)
    assert rows[0].poly_condition_id == "computed"


def test_category_sort_is_stable_and_diagnostic_only() -> None:
    politics = replace(BASE, matched_fields={"category": "politics"})
    crypto = replace(
        BASE,
        poly_condition_id="crypto",
        matched_fields={"category": "crypto"},
    )
    rows = sort_manual_review_pairs([politics, crypto], ManualReviewSort.CATEGORY)
    assert rows[0].poly_condition_id == "crypto"
