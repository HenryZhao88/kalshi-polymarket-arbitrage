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


def test_confidence_sort_ranks_highest_first() -> None:
    low = replace(BASE, poly_condition_id="low", confidence=0.61)
    high = replace(BASE, poly_condition_id="high", confidence=0.93)
    rows = sort_manual_review_pairs([low, high], ManualReviewSort.CONFIDENCE)
    assert [row.poly_condition_id for row in rows] == ["high", "low"]


def test_market_type_sort_groups_types_and_handles_missing() -> None:
    governor = replace(
        BASE,
        poly_condition_id="governor",
        matched_fields={"kalshi_market_type": "governor_winner"},
    )
    untyped = replace(BASE, poly_condition_id="untyped", confidence=0.99)
    crypto = replace(
        BASE,
        poly_condition_id="crypto",
        matched_fields={"poly_market_type": "crypto_threshold"},
    )
    rows = sort_manual_review_pairs([untyped, governor, crypto], ManualReviewSort.MARKET_TYPE)
    # Typed rows sort alphabetically; rows with no type at all sort last.
    assert [row.poly_condition_id for row in rows] == ["crypto", "governor", "untyped"]


def test_fee_confidence_sort_prefers_venue_metadata() -> None:
    unknown = replace(BASE, poly_condition_id="unknown", fee_confidence="unknown")
    metadata = replace(
        BASE,
        poly_condition_id="metadata",
        confidence=0.61,
        fee_confidence="market_metadata",
    )
    fallback = replace(
        BASE, poly_condition_id="fallback", fee_confidence="category_default"
    )
    rows = sort_manual_review_pairs(
        [unknown, fallback, metadata], ManualReviewSort.FEE_CONFIDENCE
    )
    assert [row.poly_condition_id for row in rows] == ["metadata", "fallback", "unknown"]


def test_sorting_is_stable_for_equal_keys() -> None:
    first = replace(BASE, poly_condition_id="first")
    second = replace(BASE, poly_condition_id="second")
    third = replace(BASE, poly_condition_id="third")
    for mode in ManualReviewSort:
        rows = sort_manual_review_pairs([first, second, third], mode)
        assert [row.poly_condition_id for row in rows] == ["first", "second", "third"], mode


def test_sorting_missing_values_do_not_crash_any_mode() -> None:
    bare = replace(BASE, matched_fields={}, fee_confidence="unknown")
    for mode in ManualReviewSort:
        assert sort_manual_review_pairs([bare, BASE], mode)
