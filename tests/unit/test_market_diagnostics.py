"""Manual-review ranking and outcome-entity tests; every row remains unsafe."""

from dataclasses import replace

from arb_scanner.app.markets.discovery import (
    ManualReviewSort,
    MatchedPair,
    kalshi_outcome_entity,
    normalize_entity_name,
    outcome_entities_conflict,
    poly_outcome_entity,
    sort_manual_review_pairs,
)
from arb_scanner.app.markets.polymarket import PolymarketMarket
from arb_scanner.app.markets.rule_equivalence import MatchStatus


class TestOutcomeEntityExtraction:
    def test_normalize_drops_punctuation_and_suffixes(self) -> None:
        assert normalize_entity_name("Brianne K. Nadeau") == "brianne k nadeau"
        assert normalize_entity_name("Robert White Jr.") == "robert white"
        assert normalize_entity_name("  Muriel   Bowser ") == "muriel bowser"

    def test_kalshi_custom_strike_outranks_subtitle(self) -> None:
        evidence = kalshi_outcome_entity(
            {"custom_strike": {"Candidate/Party": "Muriel Bowser"}, "yes_sub_title": "Other"}
        )
        assert evidence is not None
        assert (evidence.value, evidence.source) == ("muriel bowser", "custom_strike")

    def test_kalshi_subtitle_used_when_strike_not_name_like(self) -> None:
        evidence = kalshi_outcome_entity(
            {
                # UUID-valued strikes (e.g. GOVPARTY political_party) never extract.
                "custom_strike": {"political_party": "9244ed4c-9dfd-45cc-8211-996dc902f315"},
                "yes_sub_title": "Janeese Lewis George",
            }
        )
        assert evidence is not None
        assert (evidence.value, evidence.source) == ("janeese lewis george", "yes_sub_title")

    def test_kalshi_generic_or_numeric_subtitles_extract_nothing(self) -> None:
        assert kalshi_outcome_entity({"yes_sub_title": "Yes"}) is None
        assert kalshi_outcome_entity({"yes_sub_title": "8,000 or above"}) is None
        assert kalshi_outcome_entity({"yes_sub_title": "Republican party"}) is not None

    def test_poly_entity_from_question(self) -> None:
        market = PolymarketMarket.from_gamma(
            {
                "conditionId": "0x1",
                "question": "Will Christina Henderson win the 2026 Democratic "
                "D.C. Mayoral Primary?",
                "clobTokenIds": '["1", "2"]',
            }
        )
        evidence = poly_outcome_entity(market)
        assert evidence is not None
        assert (evidence.value, evidence.source) == ("christina henderson", "question")

    def test_poly_group_item_title_outranks_question(self) -> None:
        market = PolymarketMarket.from_gamma(
            {
                "conditionId": "0x1",
                "question": "Will Karl Racine win the primary?",
                "groupItemTitle": "Karl Racine",
                "clobTokenIds": '["1", "2"]',
            }
        )
        evidence = poly_outcome_entity(market)
        assert evidence is not None
        assert evidence.source == "group_item_title"

    def test_poly_single_token_subject_extracts_nothing(self) -> None:
        market = PolymarketMarket.from_gamma(
            {
                "conditionId": "0x1",
                "question": "Will Republicans win the South Carolina governor race?",
                "clobTokenIds": '["1", "2"]',
            }
        )
        assert poly_outcome_entity(market) is None


class TestOutcomeEntitiesConflict:
    def test_identical_names_do_not_conflict(self) -> None:
        assert not outcome_entities_conflict("brianne k nadeau", "brianne k nadeau")

    def test_subset_names_do_not_conflict(self) -> None:
        assert not outcome_entities_conflict("brianne k nadeau", "brianne nadeau")
        assert not outcome_entities_conflict("robert white", "robert white")

    def test_different_surnames_conflict(self) -> None:
        assert outcome_entities_conflict("brianne k nadeau", "christina henderson")
        assert outcome_entities_conflict("muriel bowser", "karl racine")

    def test_same_surname_different_first_name_conflicts(self) -> None:
        assert outcome_entities_conflict("robert white", "james white")

    def test_initial_only_difference_is_ambiguous_not_conflict(self) -> None:
        assert not outcome_entities_conflict("j white", "james white")

    def test_single_token_names_never_conflict(self) -> None:
        assert not outcome_entities_conflict("bowser", "racine")

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
