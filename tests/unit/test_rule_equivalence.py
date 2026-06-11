"""Rule-equivalence validation tests (matching pipeline stages 4–5).

Covers the SPEC trap cases: same title but different determination time; same game
but different void rules; sports early-start divergence; UMA challenge windows.
"""

from datetime import UTC, datetime, timedelta

from arb_scanner.app.markets.rule_equivalence import (
    KalshiRuleFacts,
    MatchStatus,
    PolymarketRuleFacts,
    basket_scope_conflict,
    continent_scope_conflict,
    crypto_performance_vs_price_threshold_conflict,
    decide_status,
    office_level_conflict,
    settlement_basis_conflict,
    sports_stage_vs_winner_conflict,
    stock_close_vs_intramonth_high_conflict,
    validate_rules,
)

T0 = datetime(2026, 6, 30, 16, 0, tzinfo=UTC)


def kalshi_facts(**overrides: object) -> KalshiRuleFacts:
    defaults: dict[str, object] = {
        "determination_time": T0,
        "resolution_source": "coindesk btc price index",
        "can_close_early": False,
        "is_sports": False,
        "void_policy": "none",
        "resolution_text": "Settles from the Coindesk BTC price index at 16:00 UTC.",
    }
    defaults.update(overrides)
    return KalshiRuleFacts(**defaults)  # type: ignore[arg-type]


def poly_facts(**overrides: object) -> PolymarketRuleFacts:
    defaults: dict[str, object] = {
        "determination_time": T0,
        "resolution_source": "coindesk btc price index",
        "uma_resolution": True,
        "is_sports": False,
        "game_start_time": None,
        "void_policy": "none",
        "resolution_text": "Settles from the Coindesk BTC price index at 16:00 UTC.",
    }
    defaults.update(overrides)
    return PolymarketRuleFacts(**defaults)  # type: ignore[arg-type]


class TestValidateRules:
    def test_equivalent_markets_pass(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert result.hard_failures == ()
        # UMA challenge window is always a warning on Polymarket-resolved markets
        assert any("uma" in w.lower() for w in result.warnings)

    def test_trap_same_title_different_determination_time(self) -> None:
        result = validate_rules(
            kalshi_facts(),
            poly_facts(determination_time=T0 + timedelta(days=1)),
        )
        assert any("determination" in f for f in result.hard_failures)

    def test_trap_same_game_different_void_rules(self) -> None:
        result = validate_rules(
            kalshi_facts(is_sports=True, void_policy="trades_stand"),
            poly_facts(is_sports=True, void_policy="refund_on_postponement"),
        )
        assert any("void" in f for f in result.hard_failures)

    def test_different_resolution_sources_fail_hard(self) -> None:
        result = validate_rules(
            kalshi_facts(resolution_source="ap election call"),
            poly_facts(resolution_source="fox news call"),
        )
        assert any("resolution source" in f for f in result.hard_failures)

    def test_missing_resolution_source_warns_not_fails(self) -> None:
        result = validate_rules(
            kalshi_facts(resolution_source=""), poly_facts(resolution_source="")
        )
        assert result.hard_failures == ()
        assert any("resolution source unverified" in w for w in result.warnings)

    def test_sports_early_start_warning(self) -> None:
        # Polymarket auto-cancels sports limit orders at game start but may miss
        # early starts; Kalshi may keep trading past close.
        result = validate_rules(
            kalshi_facts(is_sports=True, can_close_early=True),
            poly_facts(is_sports=True, game_start_time=T0 - timedelta(hours=3)),
        )
        assert any("early start" in w for w in result.warnings)

    def test_unknown_void_policy_requires_manual_review(self) -> None:
        result = validate_rules(kalshi_facts(void_policy=None), poly_facts(void_policy=None))
        assert any("void policy unknown" in warning for warning in result.warnings)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_missing_resolution_text_requires_manual_review(self) -> None:
        result = validate_rules(kalshi_facts(resolution_text=""), poly_facts(resolution_text=""))
        assert any("resolution text missing" in warning for warning in result.warnings)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_unknown_determination_time_requires_manual_review(self) -> None:
        result = validate_rules(
            kalshi_facts(determination_time=None), poly_facts(determination_time=None)
        )
        assert any("determination time unverified" in warning for warning in result.warnings)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW


# Live rules text fetched 2026-06-11 from the GOVPARTYSC-26-R market and the
# matching Polymarket market (docs/VERIFICATION.md §7).
KALSHI_GOVPARTY_RULES = (
    "If a representative of the Republican party is inaugurated as the governor "
    "of South Carolina pursuant to the 2026 election, then the market resolves to Yes."
)
POLY_GOVERNOR_RULES = (
    "This market will resolve according to the winner of the 2026 South Carolina "
    "gubernatorial election. A candidate shall be considered to represent a party "
    "in the event that he or she is the nominee of the party in question. "
    "The resolution source for this market is the Associated Press, Fox News, and "
    "NBC. This market will resolve once all three sources call the race for the "
    "same candidate."
)


class TestSettlementBasisConflict:
    """Sworn-in/inaugurated officeholder basis vs called-election-winner basis."""

    def test_detects_verified_govparty_divergence(self) -> None:
        message = settlement_basis_conflict(KALSHI_GOVPARTY_RULES, POLY_GOVERNOR_RULES)
        assert message is not None
        assert "settlement_basis_conflict" in message

    def test_validate_rules_rejects_even_with_missing_fields(self) -> None:
        # Mirrors the live rows: no determination time / source / void policy.
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_GOVPARTY_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_GOVERNOR_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("settlement_basis_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_shared_winner_basis_does_not_fire(self) -> None:
        winner_text = "Resolves according to the winner of the 2026 election."
        assert settlement_basis_conflict(winner_text, winner_text) is None

    def test_shared_officeholder_basis_does_not_fire(self) -> None:
        sworn_text = "Resolves when a candidate is sworn in as governor."
        assert settlement_basis_conflict(sworn_text, sworn_text) is None

    def test_ambiguous_kalshi_text_with_both_bases_does_not_fire(self) -> None:
        both = (
            "Resolves when the winner of the 2026 election is inaugurated as governor."
        )
        assert settlement_basis_conflict(both, POLY_GOVERNOR_RULES) is None

    def test_ambiguous_poly_text_with_both_bases_does_not_fire(self) -> None:
        both = (
            "Resolves according to the winner of the 2026 election, "
            "once that person is sworn in."
        )
        assert settlement_basis_conflict(KALSHI_GOVPARTY_RULES, both) is None

    def test_reverse_direction_does_not_fire(self) -> None:
        # Kalshi winner-basis vs Polymarket officeholder-basis is not the
        # verified GOVPARTY pattern; leave it to the other conservative checks.
        assert settlement_basis_conflict(POLY_GOVERNOR_RULES, KALSHI_GOVPARTY_RULES) is None

    def test_unrelated_rules_do_not_fire(self) -> None:
        assert (
            settlement_basis_conflict(
                "Settles from the Coindesk BTC price index at 16:00 UTC.",
                "Settles from the Coindesk BTC price index at 16:00 UTC.",
            )
            is None
        )

    def test_member_of_party_language_counts_as_officeholder_basis(self) -> None:
        contract_terms = (
            "The person sworn in to the governorship is a member of the Republican party."
        )
        assert settlement_basis_conflict(contract_terms, POLY_GOVERNOR_RULES) is not None


# Live rules text from the 2026-06-11 2,000-market dry-run
# (docs/VERIFICATION.md §8).
KALSHI_STATE_SENATE_RULES = (
    "If the Republican party wins the North Carolina State Senate in 2026, then "
    "the market resolves to Yes. Winning is defined as holding more seats than "
    "any other party."
)
POLY_US_SENATE_RULES = (
    "This market will resolve according to the winner of the 2026 midterm North "
    "Carolina U.S. Senate election, inclusive of any run-offs."
)
KALSHI_SWEEP_RULES = (
    "If Democrats win the 2026 Senate elections in ALL of the following states: "
    "Georgia, Michigan, North Carolina, AND Maine, then the market resolves to Yes."
)
POLY_NC_SENATE_RULES = (
    "This market will resolve according to the winner of the 2026 North Carolina "
    "Senate race."
)


class TestOfficeLevelConflict:
    """State legislative chamber control vs U.S. Senate race."""

    def test_state_senate_vs_us_senate_is_rejected(self) -> None:
        message = office_level_conflict(KALSHI_STATE_SENATE_RULES, POLY_US_SENATE_RULES)
        assert message is not None
        assert "office_level_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_STATE_SENATE_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_US_SENATE_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("office_level_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_democratic_variant_is_rejected(self) -> None:
        kalshi = KALSHI_STATE_SENATE_RULES.replace("Republican", "Democratic")
        assert office_level_conflict(kalshi, POLY_US_SENATE_RULES) is not None

    def test_republican_variant_is_rejected_in_either_direction(self) -> None:
        assert office_level_conflict(POLY_US_SENATE_RULES, KALSHI_STATE_SENATE_RULES) is not None

    def test_us_senate_vs_us_senate_is_not_rejected(self) -> None:
        assert office_level_conflict(POLY_US_SENATE_RULES, POLY_US_SENATE_RULES) is None

    def test_state_senate_vs_state_senate_is_not_rejected(self) -> None:
        assert (
            office_level_conflict(KALSHI_STATE_SENATE_RULES, KALSHI_STATE_SENATE_RULES) is None
        )

    def test_ambiguous_senate_with_no_level_evidence_falls_through(self) -> None:
        ambiguous = "Resolves according to the winner of the North Carolina Senate race."
        assert office_level_conflict(ambiguous, ambiguous) is None
        assert office_level_conflict(KALSHI_STATE_SENATE_RULES, ambiguous) is None
        # And without other evidence the pair stays manual_review, not rejected.
        result = validate_rules(
            kalshi_facts(
                resolution_text=ambiguous,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=ambiguous,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert result.hard_failures == ()
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_text_with_both_levels_is_ambiguous_and_falls_through(self) -> None:
        both = (
            "Covers the State Senate as well as the U.S. Senate race in North Carolina."
        )
        assert office_level_conflict(both, POLY_US_SENATE_RULES) is None


class TestBasketScopeConflict:
    """Multi-state all-must-win sweep vs single-state race."""

    def test_four_state_sweep_vs_single_race_is_rejected(self) -> None:
        message = basket_scope_conflict(KALSHI_SWEEP_RULES, POLY_NC_SENATE_RULES)
        assert message is not None
        assert "basket_scope_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_SWEEP_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_NC_SENATE_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("basket_scope_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert basket_scope_conflict(POLY_NC_SENATE_RULES, KALSHI_SWEEP_RULES) is not None

    def test_same_basket_on_both_sides_is_not_rejected(self) -> None:
        assert basket_scope_conflict(KALSHI_SWEEP_RULES, KALSHI_SWEEP_RULES) is None

    def test_candidate_name_list_is_not_a_basket(self) -> None:
        candidates = (
            "Will Alice Johnson, Bob Smith, or Carol Davis win the North Carolina "
            "Senate race in 2026?"
        )
        assert basket_scope_conflict(candidates, POLY_NC_SENATE_RULES) is None

    def test_single_state_vs_single_state_is_not_rejected(self) -> None:
        assert basket_scope_conflict(POLY_NC_SENATE_RULES, POLY_NC_SENATE_RULES) is None

    def test_zero_state_text_is_uncertain_and_falls_through(self) -> None:
        no_states = "Resolves according to the winner of the Senate race."
        assert basket_scope_conflict(KALSHI_SWEEP_RULES, no_states) is None

    def test_two_states_mentioned_without_basket_language_is_not_a_basket(self) -> None:
        # Mentioning a second state outside a conjunction chain (e.g. a
        # comparison) must not classify as a basket on its own.
        comparison = (
            "Resolves according to the winner of the 2026 North Carolina Senate "
            "race. Unlike the market for Georgia this market has no run-off clause."
        )
        assert basket_scope_conflict(comparison, POLY_NC_SENATE_RULES) is None


# Live title + rules text from the 2026-06-11 5,000-market dry-run
# (docs/VERIFICATION.md §9). The detectors receive title and rules combined.
KALSHI_CONTINENT_COMPLEMENT = (
    "Will the winner of the 2026 Men's FIFA World Cup be from any continent "
    "other than Europe or South America?\n"
    "If any country not in Europe or South America wins the 2026 Men's FIFA "
    "World Cup, then the market resolves to Yes."
)
POLY_SA_WINS_WC = (
    "Will South America win the 2026 FIFA World Cup?\n"
    "This market will resolve to the continent of the country that wins the "
    "2026 FIFA World Cup."
)
KALSHI_KNOCKOUT_COUNT = (
    "Will at least 2 teams from South America reach the knockout stage of the "
    "2026 Men's FIFA World Cup?\n"
    "If at least 2 teams from South America reach the knockout stage of the "
    "2026 Men's FIFA World Cup, then the market resolves to Yes."
)
KALSHI_BTC_THRESHOLD = (
    "Will Bitcoin be above $100000 by October 1, 2026 at 12:00AM ET?\n"
    "If the price of Bitcoin is above $100,000 at any point before October 1, "
    "2026 at 12:00 AM ET, then the market resolves to Yes."
)
POLY_BTC_BEST_MONTH = (
    "Will October be the best month for Bitcoin in 2026?\n"
    "This market will resolve to the calendar month during which Bitcoin has "
    "the highest percentage change in 2026."
)
KALSHI_SPX_CLOSE = (
    "Will the S&P 500 be above 8200 on Dec 31, 2026 at 4pm EST?\n"
    "If the S&P 500 index value on Dec 31, 2026 at 4pm EST is above 8200, "
    "then the market resolves to Yes."
)
POLY_SPX_HIGH = (
    "Will S&P 500 (SPX) hit $8,200 (HIGH) in December?\n"
    "This market will resolve to Yes if, at any point between market creation "
    "and market close on the final day of trading for December 2026, any "
    "1-minute candle trades at or above 8,200."
)
POLY_SPX_FINAL_CLOSE = (
    "Will the S&P 500 close over 8,200 in December 2026?\n"
    "This market will resolve to Yes if the S&P 500 closes over 8,200 on the "
    "final trading day of December 2026."
)


class TestContinentScopeConflict:
    def test_complement_vs_excluded_continent_is_rejected(self) -> None:
        message = continent_scope_conflict(KALSHI_CONTINENT_COMPLEMENT, POLY_SA_WINS_WC)
        assert message is not None
        assert "continent_scope_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_CONTINENT_COMPLEMENT,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_SA_WINS_WC,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("continent_scope_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert continent_scope_conflict(POLY_SA_WINS_WC, KALSHI_CONTINENT_COMPLEMENT) is not None

    def test_same_continent_winner_pair_is_not_rejected(self) -> None:
        kalshi_sa = (
            "Will the winner of the 2026 Men's FIFA World Cup be from South "
            "America (CONMEBOL)?\nIf a country from South America wins the 2026 "
            "Men's FIFA World Cup, then the market resolves to Yes."
        )
        assert continent_scope_conflict(kalshi_sa, POLY_SA_WINS_WC) is None

    def test_non_excluded_continent_falls_through(self) -> None:
        poly_africa = POLY_SA_WINS_WC.replace("South America", "Africa")
        assert continent_scope_conflict(KALSHI_CONTINENT_COMPLEMENT, poly_africa) is None

    def test_complement_vs_complement_falls_through(self) -> None:
        assert (
            continent_scope_conflict(KALSHI_CONTINENT_COMPLEMENT, KALSHI_CONTINENT_COMPLEMENT)
            is None
        )

    def test_requires_world_cup_context(self) -> None:
        no_wc = "Will the winner be from any continent other than Europe or South America?"
        assert continent_scope_conflict(no_wc, POLY_SA_WINS_WC) is None


class TestSportsStageVsWinnerConflict:
    def test_knockout_count_vs_winner_is_rejected(self) -> None:
        message = sports_stage_vs_winner_conflict(KALSHI_KNOCKOUT_COUNT, POLY_SA_WINS_WC)
        assert message is not None
        assert "sports_stage_vs_winner_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_KNOCKOUT_COUNT,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_SA_WINS_WC,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("sports_stage_vs_winner_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert sports_stage_vs_winner_conflict(POLY_SA_WINS_WC, KALSHI_KNOCKOUT_COUNT) is not None

    def test_stage_count_vs_stage_count_is_not_rejected(self) -> None:
        assert sports_stage_vs_winner_conflict(KALSHI_KNOCKOUT_COUNT, KALSHI_KNOCKOUT_COUNT) is None

    def test_winner_vs_winner_is_not_rejected(self) -> None:
        assert sports_stage_vs_winner_conflict(POLY_SA_WINS_WC, POLY_SA_WINS_WC) is None

    def test_stage_language_on_winner_side_is_ambiguous(self) -> None:
        mixed = POLY_SA_WINS_WC + "\nTeams eliminated before the knockout stage do not count."
        assert sports_stage_vs_winner_conflict(KALSHI_KNOCKOUT_COUNT, mixed) is None


class TestCryptoPerformanceVsPriceThresholdConflict:
    def test_threshold_vs_best_month_is_rejected(self) -> None:
        message = crypto_performance_vs_price_threshold_conflict(
            KALSHI_BTC_THRESHOLD, POLY_BTC_BEST_MONTH
        )
        assert message is not None
        assert "crypto_performance_vs_price_threshold_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_BTC_THRESHOLD,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_BTC_BEST_MONTH,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any(
            "crypto_performance_vs_price_threshold_conflict" in f for f in result.hard_failures
        )
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert (
            crypto_performance_vs_price_threshold_conflict(
                POLY_BTC_BEST_MONTH, KALSHI_BTC_THRESHOLD
            )
            is not None
        )

    def test_threshold_vs_same_threshold_is_not_rejected(self) -> None:
        assert (
            crypto_performance_vs_price_threshold_conflict(
                KALSHI_BTC_THRESHOLD, KALSHI_BTC_THRESHOLD
            )
            is None
        )

    def test_month_name_alone_is_not_performance_language(self) -> None:
        poly_october = (
            "Will Bitcoin trade above $100,000 in October?\nResolves to Yes if "
            "Bitcoin trades above $100,000 at any point in October 2026."
        )
        assert (
            crypto_performance_vs_price_threshold_conflict(KALSHI_BTC_THRESHOLD, poly_october)
            is None
        )

    def test_requires_crypto_asset_on_both_sides(self) -> None:
        non_crypto = "Will October be the best month for the S&P 500 in 2026?"
        assert (
            crypto_performance_vs_price_threshold_conflict(KALSHI_BTC_THRESHOLD, non_crypto)
            is None
        )


class TestStockCloseVsIntramonthHighConflict:
    def test_fixed_close_vs_intramonth_high_is_rejected(self) -> None:
        message = stock_close_vs_intramonth_high_conflict(KALSHI_SPX_CLOSE, POLY_SPX_HIGH)
        assert message is not None
        assert "stock_close_vs_intramonth_high_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_SPX_CLOSE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_SPX_HIGH,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any(
            "stock_close_vs_intramonth_high_conflict" in f for f in result.hard_failures
        )
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert stock_close_vs_intramonth_high_conflict(POLY_SPX_HIGH, KALSHI_SPX_CLOSE) is not None

    def test_fixed_close_vs_final_trading_day_close_is_not_rejected(self) -> None:
        # The potentially equivalent family from the scan: must stay
        # manual_review for source/void verification, not be high-rejected.
        assert (
            stock_close_vs_intramonth_high_conflict(KALSHI_SPX_CLOSE, POLY_SPX_FINAL_CLOSE)
            is None
        )
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_SPX_CLOSE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_SPX_FINAL_CLOSE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert result.hard_failures == ()
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_high_vs_high_is_not_rejected(self) -> None:
        assert stock_close_vs_intramonth_high_conflict(POLY_SPX_HIGH, POLY_SPX_HIGH) is None

    def test_close_vs_close_is_not_rejected(self) -> None:
        assert (
            stock_close_vs_intramonth_high_conflict(KALSHI_SPX_CLOSE, KALSHI_SPX_CLOSE) is None
        )

    def test_requires_index_context_on_both_sides(self) -> None:
        no_index = "Will the value hit $8,200 (HIGH) in December at any point?"
        assert stock_close_vs_intramonth_high_conflict(KALSHI_SPX_CLOSE, no_index) is None

    def test_unclassifiable_side_falls_through(self) -> None:
        vague = "Will the S&P 500 be above 8,200 in December 2026?"
        assert stock_close_vs_intramonth_high_conflict(vague, POLY_SPX_HIGH) is None


class TestDecideStatus:
    def test_high_confidence_clean_rules_accepted(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_hard_failure_rejected_even_at_high_similarity(self) -> None:
        result = validate_rules(
            kalshi_facts(), poly_facts(determination_time=T0 + timedelta(days=1))
        )
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.REJECTED

    def test_low_similarity_rejected(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.2, rules=result) is MatchStatus.REJECTED

    def test_mid_similarity_goes_to_manual_review(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.75, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_many_warnings_cap_at_manual_review(self) -> None:
        result = validate_rules(
            kalshi_facts(is_sports=True, can_close_early=True, resolution_source=""),
            poly_facts(
                is_sports=True,
                game_start_time=T0 - timedelta(hours=3),
                resolution_source="",
            ),
        )
        assert len(result.warnings) >= 3
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.MANUAL_REVIEW
