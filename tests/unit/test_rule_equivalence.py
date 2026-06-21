"""Rule-equivalence validation tests (matching pipeline stages 4–5).

Covers the SPEC trap cases: same title but different determination time; same game
but different void rules; sports early-start divergence; UMA challenge windows.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from arb_scanner.app.markets.rule_equivalence import (
    KalshiRuleFacts,
    MatchStatus,
    PolymarketRuleFacts,
    basket_scope_conflict,
    cancellation_policy_basis,
    cancellation_policy_terms,
    candidate_set_conflict,
    central_bank_decision,
    central_bank_direction_conflict,
    continent_scope_conflict,
    crypto_performance_vs_price_threshold_conflict,
    decide_status,
    extract_candidate_slate,
    office_level_conflict,
    player_prop_kind,
    player_prop_scope_conflict,
    settlement_basis_conflict,
    source_finalization_basis,
    source_finalization_terms,
    sports_stage_vs_winner_conflict,
    stat_tie_policy,
    stock_close_vs_intramonth_high_conflict,
    validate_rules,
    void_policy_conflict,
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

    def test_trap_same_title_close_determination_times_is_risk_flag(self) -> None:
        # A small timing difference (trading cutoff vs UMA end date, buffer
        # days) is a settlement risk flag, not proof of a different event.
        result = validate_rules(
            kalshi_facts(),
            poly_facts(determination_time=T0 + timedelta(days=1)),
        )
        assert result.hard_failures == ()
        assert any("determination time differs" in f for f in result.risk_flags)

    def test_trap_same_title_materially_different_determination_time(self) -> None:
        # Horizons more than a week apart imply a different resolution question.
        result = validate_rules(
            kalshi_facts(),
            poly_facts(determination_time=T0 + timedelta(days=30)),
        )
        assert any("determination" in f for f in result.hard_failures)

    def test_trap_same_game_different_void_rules_is_risk_flag(self) -> None:
        # Void handling only diverges in the void/cancellation tail; the normal
        # outcome is identical, so it is a risk flag for the human to verify.
        result = validate_rules(
            kalshi_facts(is_sports=True, void_policy="trades_stand"),
            poly_facts(is_sports=True, void_policy="refund_on_postponement"),
        )
        assert result.hard_failures == ()
        assert any("void policy differs" in f for f in result.risk_flags)

    def test_different_resolution_sources_are_risk_flag(self) -> None:
        # Cross-venue source wording differs even for the same source; flag for
        # verification rather than reject.
        result = validate_rules(
            kalshi_facts(resolution_source="ap election call"),
            poly_facts(resolution_source="fox news call"),
        )
        assert result.hard_failures == ()
        assert any("resolution source differs" in f for f in result.risk_flags)

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

    def test_unknown_void_policy_accepts_with_risk_flag(self) -> None:
        result = validate_rules(kalshi_facts(void_policy=None), poly_facts(void_policy=None))
        assert any("void policy unknown" in flag for flag in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_missing_resolution_text_accepts_with_risk_flag(self) -> None:
        result = validate_rules(kalshi_facts(resolution_text=""), poly_facts(resolution_text=""))
        assert any("resolution text missing" in flag for flag in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_unknown_determination_time_accepts_with_risk_flag(self) -> None:
        result = validate_rules(
            kalshi_facts(determination_time=None), poly_facts(determination_time=None)
        )
        assert any("determination time unverified" in flag for flag in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED


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
        # No different-event conflict: accepts with settlement risk flags, never
        # hard-rejected on this ambiguous office evidence.
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
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

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
# Real Gamma description (condition 0x0ed2e5e9…, fetched 2026-06-11): the
# rules text names OTHER continents as examples, so only the title can
# identify which continent this market is.
POLY_SA_WINS_WC = (
    "Will South America win the 2026 FIFA World Cup?\n"
    "This market will resolve to the continent of the country that wins the "
    "2026 FIFA World Cup, currently scheduled for June 11-July 19, 2026. "
    "For example, if France wins the tournament, the market will resolve to "
    "Europe. If the 2026 FIFA World Cup is cancelled, postponed after "
    "December 31, 2026, or there is otherwise no winner declared within that "
    "timeframe, this market will resolve to “Other”."
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

    def test_no_clause_complement_phrasing_is_not_an_exclusion(self) -> None:
        # An equivalent single-continent market whose rules state the No side
        # as a complement must not be read as a complement market.
        kalshi_sa_no_clause = (
            "Will South America (CONMEBOL) win the 2026 Men's World Cup?\n"
            "If a country from South America wins the 2026 Men's FIFA World Cup, "
            "then the market resolves to Yes. If any country not in South America "
            "wins the 2026 Men's FIFA World Cup, then the market resolves to No."
        )
        assert continent_scope_conflict(kalshi_sa_no_clause, POLY_SA_WINS_WC) is None

    def test_yes_clause_complement_still_detected_after_a_no_clause(self) -> None:
        # The genuine complement market keeps firing even with a No sentence
        # elsewhere in its rules.
        kalshi_with_no_clause = (
            KALSHI_CONTINENT_COMPLEMENT
            + " If a country from Europe or South America wins, the market resolves to No."
        )
        assert continent_scope_conflict(kalshi_with_no_clause, POLY_SA_WINS_WC) is not None


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
        # The potentially equivalent close-vs-close family: no different-event
        # conflict, so it accepts with settlement risk flags for the human to
        # verify (source/void), never high-rejected.
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
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

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


# Verbatim cancellation-policy excerpts fetched 2026-06-11
# (docs/VERIFICATION.md §10). The Kalshi text is from the ACHIEVEMENTS
# contract terms (SOCCER.pdf); the Polymarket text is the live description of
# the South America World Cup market (condition 0x0ed2e5e9…).
KALSHI_FAIR_VALUE_CANCELLATION = (
    "If the final event necessary for determining the result is cancelled "
    "outright before the final event is concluded, then the markets for "
    "eligible participants will resolve so “Yes” holders receive the last "
    "traded price prior to cancellation and “No” holders receive $1 minus the "
    "Yes payout. If a last traded price is not available, the Outcome Review "
    "Committee will be responsible for making a binding determination of fair "
    "allocation. If a fair allocation is not able to be reliably determined, "
    "then the markets will resolve so “Yes” holders receive $1/[the number of "
    "eligible participants remaining] rounded down to the nearest cent."
)
POLY_RESOLVES_TO_OTHER = (
    "If the 2026 FIFA World Cup is cancelled, postponed after December 31, "
    "2026, or there is otherwise no winner declared within that timeframe, "
    "this market will resolve to “Other”."
)
KALSHI_WC_CONTINENT_RULES = (
    "If any country that competes in South America (CONMEBOL) qualification "
    "is the 2026 FIFA Men's World Cup champion, then the market resolves to "
    "Yes. For settlement purposes, a country is considered part of a "
    "continent based on the FIFA World Cup qualification pathway it competes "
    "through, rather than strict geographic borders."
)


class TestCancellationPolicyBasis:
    def test_kalshi_fair_value_terms_and_basis(self) -> None:
        terms = cancellation_policy_terms(KALSHI_FAIR_VALUE_CANCELLATION)
        assert "fair_value" in terms  # via "last traded price"
        assert "committee_review" in terms
        assert "split_or_1_over_n" in terms
        assert "cancellation" in terms
        assert cancellation_policy_basis(KALSHI_FAIR_VALUE_CANCELLATION) == (
            "fair_value_settlement"
        )

    def test_polymarket_resolves_to_other_terms_and_basis(self) -> None:
        terms = cancellation_policy_terms(POLY_RESOLVES_TO_OTHER)
        assert "resolves_to_other" in terms
        assert "hard_no_on_other" in terms
        assert "cancellation" in terms
        assert "postponement_deadline" in terms
        assert cancellation_policy_basis(POLY_RESOLVES_TO_OTHER) == "resolves_to_other"

    def test_text_without_cancellation_language_has_no_basis(self) -> None:
        assert cancellation_policy_basis(KALSHI_WC_CONTINENT_RULES) is None
        assert cancellation_policy_terms(KALSHI_WC_CONTINENT_RULES) == ()

    def test_text_with_both_families_is_ambiguous(self) -> None:
        both = KALSHI_FAIR_VALUE_CANCELLATION + " " + POLY_RESOLVES_TO_OTHER
        assert cancellation_policy_basis(both) is None


class TestVoidPolicyConflict:
    def test_proven_fair_value_vs_resolves_to_other_is_risk_flag(self) -> None:
        # Same event; the bases agree in normal resolution and diverge only if
        # the event is cancelled. Operator model (§18): a cancellation-tail
        # divergence is a risk flag to verify, not a different-event rejection.
        message = void_policy_conflict(KALSHI_FAIR_VALUE_CANCELLATION, POLY_RESOLVES_TO_OTHER)
        assert message is not None
        assert "void_policy_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_FAIR_VALUE_CANCELLATION,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_RESOLVES_TO_OTHER,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert result.hard_failures == ()
        assert any("void_policy_conflict" in f for f in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_fires_in_either_direction(self) -> None:
        assert (
            void_policy_conflict(POLY_RESOLVES_TO_OTHER, KALSHI_FAIR_VALUE_CANCELLATION)
            is not None
        )

    def test_same_fair_value_policy_does_not_conflict(self) -> None:
        assert (
            void_policy_conflict(
                KALSHI_FAIR_VALUE_CANCELLATION, KALSHI_FAIR_VALUE_CANCELLATION
            )
            is None
        )

    def test_same_resolves_to_other_policy_does_not_conflict(self) -> None:
        assert void_policy_conflict(POLY_RESOLVES_TO_OTHER, POLY_RESOLVES_TO_OTHER) is None

    def test_one_sided_extraction_stays_manual_review_with_mismatch_warning(self) -> None:
        # The live KXWCCONTINENT-26-SA shape: Kalshi's fair-value handling is
        # in series contract terms the scanner never sees, so only the
        # Polymarket basis is provable. Must NOT hard-reject.
        assert void_policy_conflict(KALSHI_WC_CONTINENT_RULES, POLY_RESOLVES_TO_OTHER) is None
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_WC_CONTINENT_RULES,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_RESOLVES_TO_OTHER,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert not any("void_policy_conflict" in f for f in result.hard_failures)
        assert any(
            "void_policy_mismatch: kalshi=unknown polymarket=resolves_to_other" in w
            for w in result.warnings
        )
        assert "void_policy_basis" in result.missing_fields
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_both_sides_unknown_get_no_mismatch_warning(self) -> None:
        result = validate_rules(
            kalshi_facts(resolution_text=KALSHI_WC_CONTINENT_RULES),
            poly_facts(resolution_text=KALSHI_WC_CONTINENT_RULES),
        )
        assert not any("void_policy_mismatch" in w for w in result.warnings)

    def test_void_mismatch_accepts_with_risk_flag(self) -> None:
        # The mismatch is recorded as a risk flag the human must clear; it no
        # longer blocks the opportunity from reaching economics.
        result = validate_rules(
            kalshi_facts(resolution_text=KALSHI_WC_CONTINENT_RULES),
            poly_facts(resolution_text=POLY_RESOLVES_TO_OTHER),
        )
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.ACCEPTED
        assert any("void_policy_mismatch" in f for f in result.risk_flags)


# Verbatim source/finalization excerpts fetched 2026-06-11
# (docs/VERIFICATION.md §11).
KALSHI_SPX_SNAPSHOT = (
    "Will the S&P 500 be above 8000 on Dec 31, 2026 at 4pm EST?\n"
    "If the S&P 500 index value on Dec 31, 2026 at 4pm EST is above 8000, "
    "then the market resolves to Yes."
)
POLY_SPX_OFFICIAL_CLOSE = (
    "Will S&P 500 (SPX) close over $8,000 on the final trading day of December 2026?\n"
    "This market will resolve to \"Yes\" if the official closing price for "
    "S&P 500 (SPX) on the final trading day of December 2026 is higher than "
    "the listed price. If no official closing price is published for that "
    "session, the market will use the last valid on-exchange trade price of "
    "the regular session as the effective closing price. The resolution "
    "source for this market is Yahoo Finance, specifically the S&P 500 (SPX) "
    "\"Close\" prices available under \"Historical Prices.\""
)


class TestSourceFinalizationBasis:
    def test_kalshi_snapshot_terms_and_basis(self) -> None:
        terms = source_finalization_terms(KALSHI_SPX_SNAPSHOT)
        assert "fixed_time_snapshot" in terms
        assert source_finalization_basis(KALSHI_SPX_SNAPSHOT) == "fixed_time_snapshot"

    def test_polymarket_official_close_terms_and_basis(self) -> None:
        terms = source_finalization_terms(POLY_SPX_OFFICIAL_CLOSE)
        assert "official_close" in terms
        assert "yahoo_finance_close" in terms
        assert "historical_close" in terms
        assert "last_valid_trade" in terms
        assert source_finalization_basis(POLY_SPX_OFFICIAL_CLOSE) == "official_close"

    def test_contract_terms_language_counts_as_snapshot_family(self) -> None:
        terms_text = (
            "The Source Agency is Kalshi. Revisions to the Underlying made "
            "after Expiration will not be accounted for. If no data is "
            "available, the Expiration Value will be the value most recently "
            "available prior to that time."
        )
        terms = source_finalization_terms(terms_text)
        assert "source_agency_kalshi" in terms
        assert "revisions_ignored_after_expiration" in terms
        assert "no_data_extension" in terms
        assert source_finalization_basis(terms_text) == "fixed_time_snapshot"

    def test_wording_only_text_has_no_basis(self) -> None:
        assert source_finalization_basis("Settles based on the S&P 500 in December.") is None

    def test_both_families_is_ambiguous(self) -> None:
        both = KALSHI_SPX_SNAPSHOT + " " + POLY_SPX_OFFICIAL_CLOSE
        assert source_finalization_basis(both) is None


class TestSourceFinalizationMismatch:
    def _result(self, kalshi_text: str, poly_text: str) -> Any:
        return validate_rules(
            kalshi_facts(
                resolution_text=kalshi_text,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=poly_text,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )

    def test_snapshot_vs_official_close_warns_and_accepts_with_risk_flag(self) -> None:
        result = self._result(KALSHI_SPX_SNAPSHOT, POLY_SPX_OFFICIAL_CLOSE)
        assert result.hard_failures == ()
        assert any(
            "source_finalization_mismatch: kalshi=fixed_time_snapshot "
            "polymarket=official_close" in w
            for w in result.risk_flags
        )
        assert "source_finalization_basis" in result.missing_fields
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_same_snapshot_basis_does_not_warn(self) -> None:
        result = self._result(KALSHI_SPX_SNAPSHOT, KALSHI_SPX_SNAPSHOT)
        assert not any("source_finalization_mismatch" in w for w in result.warnings)

    def test_same_official_close_basis_does_not_warn(self) -> None:
        result = self._result(POLY_SPX_OFFICIAL_CLOSE, POLY_SPX_OFFICIAL_CLOSE)
        assert not any("source_finalization_mismatch" in w for w in result.warnings)

    def test_ambiguous_or_missing_source_text_does_not_warn(self) -> None:
        vague = "Settles based on the S&P 500 level in December 2026."
        result = self._result(vague, POLY_SPX_OFFICIAL_CLOSE)
        assert not any("source_finalization_mismatch" in w for w in result.warnings)

    def test_warning_accepts_with_risk_flag(self) -> None:
        result = self._result(KALSHI_SPX_SNAPSHOT, POLY_SPX_OFFICIAL_CLOSE)
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.ACCEPTED
        assert any("source_finalization_mismatch" in f for f in result.risk_flags)

    def test_existing_hard_rejection_overrides(self) -> None:
        # A material determination-horizon gap (>7d) is a different-event hard
        # failure and overrides the source-finalization risk flag.
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_SPX_SNAPSHOT,
                determination_time=T0,
            ),
            poly_facts(
                resolution_text=POLY_SPX_OFFICIAL_CLOSE,
                determination_time=T0 + timedelta(days=30),
            ),
        )
        assert result.hard_failures
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.REJECTED


# Verbatim sweep rules fetched 2026-06-11 (docs/VERIFICATION.md §12).
KALSHI_PROGRESSIVE_SLATE = (
    "Will the listed Democratic Senate candidates all win their primary elections?\n"
    "If ALL of the following Democratic candidates win their 2026 Senate primary "
    "elections: Juliana Stratton in Illinois, Graham Platner in Maine, Mallory "
    "McMorrow OR Abdul El-Sayed in Michigan, Peggy Flanagan in Minnesota, and "
    "Ed Markey in Massachusetts, then the market resolves to Yes."
)
POLY_INCUMBENT_COHORT = (
    "Will Democratic Senate incumbents win all their nominating elections in the "
    "2026 cycle?\n"
    "This market will resolve according to the number of Democratic Senate "
    "incumbents who do not win their nominating election to move on to the "
    "general election as a result of the 2026 midterm primary elections. "
    "Incumbents who do not officially register as candidates for reelection "
    "will not be considered."
)


class TestExtractCandidateSlate:
    def test_extracts_named_groups_with_or_alternatives(self) -> None:
        slate = extract_candidate_slate(KALSHI_PROGRESSIVE_SLATE)
        assert len(slate) == 5
        flattened = {name for group in slate for name in group}
        assert "juliana stratton" in flattened
        assert "ed markey" in flattened
        # The OR clause is one group of two interchangeable alternatives.
        or_groups = [group for group in slate if len(group) == 2]
        assert or_groups == [frozenset({"mallory mcmorrow", "abdul el-sayed"})]

    def test_cohort_text_extracts_no_slate(self) -> None:
        assert extract_candidate_slate(POLY_INCUMBENT_COHORT) == frozenset()


class TestCandidateSetConflict:
    def test_named_slate_vs_incumbent_cohort_is_rejected(self) -> None:
        message = candidate_set_conflict(KALSHI_PROGRESSIVE_SLATE, POLY_INCUMBENT_COHORT)
        assert message is not None
        assert "candidate_set_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_PROGRESSIVE_SLATE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_INCUMBENT_COHORT,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("candidate_set_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_fires_in_either_direction(self) -> None:
        assert (
            candidate_set_conflict(POLY_INCUMBENT_COHORT, KALSHI_PROGRESSIVE_SLATE) is not None
        )

    def test_different_named_slates_are_rejected(self) -> None:
        other_slate = KALSHI_PROGRESSIVE_SLATE.replace("Juliana Stratton", "Alex Johnson")
        assert candidate_set_conflict(KALSHI_PROGRESSIVE_SLATE, other_slate) is not None

    def test_identical_slates_are_not_rejected(self) -> None:
        assert (
            candidate_set_conflict(KALSHI_PROGRESSIVE_SLATE, KALSHI_PROGRESSIVE_SLATE) is None
        )

    def test_one_side_missing_slate_without_cohort_is_mismatch_not_rejection(self) -> None:
        vague_sweep = (
            "Will the listed Democratic Senate candidates all win their primary "
            "elections? Resolves Yes if every listed candidate wins."
        )
        assert candidate_set_conflict(KALSHI_PROGRESSIVE_SLATE, vague_sweep) is None
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_PROGRESSIVE_SLATE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=vague_sweep,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert not any("candidate_set_conflict" in f for f in result.hard_failures)
        assert any("candidate_set_mismatch" in w for w in result.warnings)
        assert "candidate_set" in result.missing_fields
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_non_sweep_markets_never_fire(self) -> None:
        single = "Will Alice Johnson win the Michigan Senate primary?"
        assert candidate_set_conflict(single, POLY_INCUMBENT_COHORT) is None

    def test_mismatch_accepts_with_risk_flag(self) -> None:
        vague_sweep = (
            "Will the listed candidates all win their primary elections? "
            "Resolves Yes if every listed candidate wins."
        )
        result = validate_rules(
            kalshi_facts(resolution_text=KALSHI_PROGRESSIVE_SLATE),
            poly_facts(resolution_text=vague_sweep),
        )
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.ACCEPTED
        assert any("candidate_set_mismatch" in f for f in result.risk_flags)


# Live titles from the 2026-06-11 10k-market dry-run (docs/VERIFICATION.md §14).
KALSHI_MVP = "Will Yoshinobu Yamamoto win NL MVP?"
KALSHI_WINS_LEADER = (
    "Will Yoshinobu Yamamoto lead Pro Baseball in wins for the 2026 regular season?"
)
KALSHI_K_LEADER = (
    "Will Yoshinobu Yamamoto lead Pro Baseball in strikeouts for the 2026 regular season?"
)
POLY_K_LEADER = (
    "Will Yoshinobu Yamamoto strike out the most batters during the 2026 MLB regular season?"
)
POLY_TRADED = "Will Brian Thomas Jr. be traded?"
KALSHI_REC_YDS_THRESHOLD = (
    "Will Brian Thomas Jr. record 1000+ receiving yards during 2026-27 Pro Football "
    "regular season?"
)
KALSHI_REC_YDS_LEADER = (
    "Will Brian Thomas Jr. lead Pro Football in Receiving Yards for the 2026-2027 "
    "regular season?"
)


class TestPlayerPropKind:
    def test_award_kinds(self) -> None:
        assert player_prop_kind(KALSHI_MVP) == "award_winner"
        assert (
            player_prop_kind("Will Kayvon Thibodeaux win the Defensive Player of the Year?")
            == "award_winner"
        )
        assert (
            player_prop_kind("Will Trent Williams be #1 on the Pro Football Top 100 List?")
            == "award_winner"
        )

    def test_stat_leader_kinds_normalize_phrasings(self) -> None:
        # Both venue phrasings of the same statistic map to the same kind.
        assert player_prop_kind(KALSHI_K_LEADER) == "stat_leader:strikeouts"
        assert player_prop_kind(POLY_K_LEADER) == "stat_leader:strikeouts"
        assert player_prop_kind(KALSHI_WINS_LEADER) == "stat_leader:wins"

    def test_transaction_and_threshold_kinds(self) -> None:
        assert player_prop_kind(POLY_TRADED) == "transaction"
        assert player_prop_kind(KALSHI_REC_YDS_THRESHOLD) == "stat_threshold"

    def test_plain_text_has_no_kind(self) -> None:
        assert player_prop_kind("Will Boston beat New York?") is None

    def test_multiple_kinds_are_ambiguous(self) -> None:
        assert (
            player_prop_kind("Will Yamamoto win NL MVP and lead Pro Baseball in wins?") is None
        )


class TestPlayerPropScopeConflict:
    def test_award_vs_stat_leader_is_rejected(self) -> None:
        message = player_prop_scope_conflict(KALSHI_MVP, POLY_K_LEADER)
        assert message is not None
        assert "player_prop_scope_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_MVP,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_K_LEADER,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("player_prop_scope_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_different_stat_leaders_conflict(self) -> None:
        # wins leader vs strikeouts leader: same player, different bet.
        assert player_prop_scope_conflict(KALSHI_WINS_LEADER, POLY_K_LEADER) is not None

    def test_same_stat_leader_falls_through(self) -> None:
        # The potentially equivalent family: Kalshi "lead in strikeouts" vs
        # Polymarket "strike out the most batters". Same statistic — never
        # rejected by this rule.
        assert player_prop_scope_conflict(KALSHI_K_LEADER, POLY_K_LEADER) is None

    def test_award_vs_transaction_is_rejected(self) -> None:
        assert (
            player_prop_scope_conflict(
                "Will Kayvon Thibodeaux win the Defensive Player of the Year?",
                "Will Kayvon Thibodeaux be traded?",
            )
            is not None
        )

    def test_stat_threshold_vs_transaction_is_rejected(self) -> None:
        assert player_prop_scope_conflict(KALSHI_REC_YDS_THRESHOLD, POLY_TRADED) is not None

    def test_stat_leader_vs_transaction_is_rejected(self) -> None:
        assert player_prop_scope_conflict(KALSHI_REC_YDS_LEADER, POLY_TRADED) is not None

    def test_same_kind_pairs_fall_through(self) -> None:
        assert player_prop_scope_conflict(KALSHI_MVP, KALSHI_MVP) is None
        assert player_prop_scope_conflict(POLY_TRADED, POLY_TRADED) is None

    def test_unclassified_side_falls_through(self) -> None:
        assert player_prop_scope_conflict(KALSHI_MVP, "Will Boston beat New York?") is None

    def test_ambiguous_side_falls_through(self) -> None:
        both = "Will Yamamoto win NL MVP and lead Pro Baseball in wins?"
        assert player_prop_scope_conflict(both, POLY_TRADED) is None


# Live KXCBDECISIONMEXICO text from the 2026-06-11 10k run
# (docs/VERIFICATION.md §16). The Polymarket rules enumerate every outcome,
# so direction must be classified from the title line.
KALSHI_CB_CUT25 = (
    "Will the Bank of Mexico Cut 25bps at the June Bank of Mexico Governing "
    "Board meeting?\n"
    "If the Bank of Mexico takes the action of Cut 25bps at June Bank of "
    "Mexico Governing Board meeting, then the market resolves to Yes. The "
    "market resolves based on the official policy rate decision."
)
POLY_CB_INCREASE = (
    "Will the Bank of Mexico announce an increase at the June meeting?\n"
    "This market will resolve according to the change in the target for the "
    "overnight interbank interest rate as a result of the monetary policy "
    "decision of the Bank of Mexico's June 2026 meeting. It will resolve to "
    "Increase if the rate is raised, Decrease if the rate is lowered, and No "
    "Change otherwise."
)
POLY_CB_DECREASE = POLY_CB_INCREASE.replace(
    "announce an increase", "announce a decrease"
)


class TestCentralBankDecision:
    def test_kalshi_cut_with_magnitude(self) -> None:
        assert central_bank_decision(KALSHI_CB_CUT25) == ("cut", "25bps")

    def test_kalshi_or_more_magnitude(self) -> None:
        text = "Will the Bank of Mexico Cut 50bps+ at the June meeting?"
        assert central_bank_decision(text) == ("cut", "50bps_or_more")

    def test_poly_direction_from_title_despite_enumerating_rules(self) -> None:
        # The rules text names increase, decrease, AND no change — the title
        # line must win or the live family classifies as ambiguous.
        assert central_bank_decision(POLY_CB_INCREASE) == ("hike", "any")
        assert central_bank_decision(POLY_CB_DECREASE) == ("cut", "any")

    def test_no_central_bank_context_extracts_nothing(self) -> None:
        assert central_bank_decision("Will the Yankees increase their lead?") is None

    def test_ambiguous_title_and_rules_extract_nothing(self) -> None:
        ambiguous = (
            "Bank of Mexico June decision?\nResolves to Increase if raised, "
            "Decrease if lowered."
        )
        assert central_bank_decision(ambiguous) is None


class TestCentralBankDirectionConflict:
    def test_cut_vs_increase_is_rejected(self) -> None:
        message = central_bank_direction_conflict(KALSHI_CB_CUT25, POLY_CB_INCREASE)
        assert message is not None
        assert "central_bank_direction_conflict" in message
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_CB_CUT25,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_CB_INCREASE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert any("central_bank_direction_conflict" in f for f in result.hard_failures)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.REJECTED

    def test_hold_vs_move_is_rejected(self) -> None:
        hold = "Will the Bank of Mexico hold rates unchanged at the June meeting?"
        assert central_bank_direction_conflict(hold, POLY_CB_INCREASE) is not None

    def test_same_direction_same_magnitude_not_rejected(self) -> None:
        assert central_bank_direction_conflict(KALSHI_CB_CUT25, KALSHI_CB_CUT25) is None

    def test_same_direction_different_magnitude_is_diagnostic_only(self) -> None:
        # Cut 25bps vs any decrease: overlapping but not equivalent.
        assert central_bank_direction_conflict(KALSHI_CB_CUT25, POLY_CB_DECREASE) is None
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_CB_CUT25,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_CB_DECREASE,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert not any("central_bank" in f for f in result.hard_failures)
        assert any("central_bank_magnitude_mismatch" in w for w in result.warnings)
        assert "central_bank_magnitude" in result.missing_fields
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_ambiguous_side_falls_through(self) -> None:
        vague = "Bank of Mexico June 2026 meeting outcome market"
        assert central_bank_direction_conflict(vague, POLY_CB_INCREASE) is None

    def test_magnitude_mismatch_accepts_with_risk_flag(self) -> None:
        result = validate_rules(
            kalshi_facts(resolution_text=KALSHI_CB_CUT25),
            poly_facts(resolution_text=POLY_CB_DECREASE),
        )
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.ACCEPTED
        assert any("central_bank_magnitude_mismatch" in f for f in result.risk_flags)


# Verbatim rules from the verified Skubal pair, fetched 2026-06-12
# (docs/VERIFICATION.md §17).
KALSHI_K_LEADER_RULES = (
    "Will Tarik Skubal lead Pro Baseball in strikeouts for the 2026 regular season?\n"
    "If Tarik Skubal leads Pro Baseball in strikeouts for the 2026 regular "
    "season, then the market resolves to Yes. The participant must have the "
    "highest total of the specified statistic across the entire season type as "
    "documented by the official league statistics. In case of exact ties where "
    "the league does not declare a single winner, tied participants receive a "
    "proportional payout."
)
POLY_K_LEADER_RULES = (
    "Will Tarik Skubal strike out the most batters during the 2026 MLB regular season?\n"
    "This market will resolve according to the pitcher who records the most "
    "strikeouts among pitchers during the 2026 Major League Baseball regular "
    "season. In the event of a tie, this market will resolve according to the "
    "official leader as determined by the rules of the MLB. If multiple leaders "
    "are announced then this market will resolve to the pitcher that records "
    "fewer innings pitched. If a tie still persists, this market will resolve "
    "to the pitcher whose listed last name comes first alphabetically."
)


class TestStatTiePolicy:
    def test_kalshi_proportional_payout_is_ties_split(self) -> None:
        assert stat_tie_policy(KALSHI_K_LEADER_RULES) == "ties_split"

    def test_polymarket_cascade_is_sole_winner(self) -> None:
        assert stat_tie_policy(POLY_K_LEADER_RULES) == "sole_winner_tiebreak"

    def test_absent_or_ambiguous_text_has_no_policy(self) -> None:
        assert stat_tie_policy("Resolves to the strikeout leader.") is None
        both = KALSHI_K_LEADER_RULES + " " + POLY_K_LEADER_RULES
        assert stat_tie_policy(both) is None


class TestStatLeaderRuleMismatch:
    def _result(self, kalshi_text: str, poly_text: str) -> Any:
        return validate_rules(
            kalshi_facts(
                resolution_text=kalshi_text,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=poly_text,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )

    def test_split_vs_sole_winner_accepts_with_risk_flag(self) -> None:
        result = self._result(KALSHI_K_LEADER_RULES, POLY_K_LEADER_RULES)
        assert not any("stat_leader" in f for f in result.hard_failures)
        assert any(
            "stat_leader_rule_mismatch: tie policy kalshi=ties_split "
            "polymarket=sole_winner_tiebreak" in w
            for w in result.risk_flags
        )
        assert "stat_leader_tie_policy" in result.missing_fields
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_unknown_tie_policy_on_same_stat_pair_is_flagged(self) -> None:
        bare = "Will Tarik Skubal lead Pro Baseball in strikeouts for the 2026 season?"
        result = self._result(bare, POLY_K_LEADER_RULES)
        assert any("stat_leader_rule_mismatch" in w for w in result.warnings)

    def test_matching_tie_policies_do_not_warn(self) -> None:
        result = self._result(POLY_K_LEADER_RULES, POLY_K_LEADER_RULES)
        assert not any("stat_leader_rule_mismatch" in w for w in result.warnings)

    def test_non_stat_leader_pairs_never_warn(self) -> None:
        result = self._result("Will Boston beat New York?", "Will Boston beat New York?")
        assert not any("stat_leader_rule_mismatch" in w for w in result.warnings)

    def test_tie_policy_mismatch_accepts_with_risk_flag(self) -> None:
        result = self._result(KALSHI_K_LEADER_RULES, POLY_K_LEADER_RULES)
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.ACCEPTED
        assert any("stat_leader_rule_mismatch" in f for f in result.risk_flags)


class TestAllStarSelectionIsAward:
    def test_all_star_selection_classifies_as_award(self) -> None:
        assert (
            player_prop_kind("Will Cristopher Sánchez be selected to the 2026 NL All-Star Team?")
            == "award_winner"
        )

    def test_all_star_vs_strikeout_leader_is_rejected(self) -> None:
        message = player_prop_scope_conflict(
            "Will Yoshinobu Yamamoto be selected to the 2026 NL All-Star Team?",
            POLY_K_LEADER,
        )
        assert message is not None
        assert "player_prop_scope_conflict" in message


class TestRiskFlagAcceptance:
    """Same-event + risk-flags acceptance model (operator decision 2026-06-21,
    docs/VERIFICATION.md §18).

    A high-similarity pair with no *different-event* conflict is ACCEPTED so its
    economics are evaluated; unverifiable or divergent *settlement mechanics*
    (resolution source, void policy, UMA challenge window, close-timestamp
    differences within a window) ride along as ``risk_flags`` for the human to
    verify rather than silently blocking the opportunity. Different-event
    conflicts and materially different determination horizons still hard-reject.
    """

    def test_missing_settlement_facts_accept_with_risk_flags(self) -> None:
        # The dominant live shape: no determination time / source / void policy
        # parseable on either venue. Previously stuck at manual_review forever.
        result = validate_rules(
            kalshi_facts(determination_time=None, resolution_source="", void_policy=None),
            poly_facts(determination_time=None, resolution_source="", void_policy=None),
        )
        assert result.hard_failures == ()
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED
        flags = " ".join(result.risk_flags).lower()
        assert "resolution source" in flags
        assert "void policy" in flags
        assert "determination time" in flags
        assert "uma" in flags

    def test_different_event_conflict_still_rejects(self) -> None:
        # office_level_conflict proves the venues resolve on different offices.
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
        assert result.hard_failures
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.REJECTED

    def test_resolution_source_difference_is_risk_flag_not_rejection(self) -> None:
        result = validate_rules(
            kalshi_facts(resolution_source="ap election call"),
            poly_facts(resolution_source="fox news call"),
        )
        assert result.hard_failures == ()
        assert any("resolution source" in f.lower() for f in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_determination_time_within_window_is_risk_flag(self) -> None:
        result = validate_rules(
            kalshi_facts(), poly_facts(determination_time=T0 + timedelta(days=1))
        )
        assert result.hard_failures == ()
        assert any("determination time" in f.lower() for f in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_determination_time_materially_apart_is_hard_failure(self) -> None:
        # A month apart strongly implies a different resolution horizon/event.
        result = validate_rules(
            kalshi_facts(), poly_facts(determination_time=T0 + timedelta(days=30))
        )
        assert any("determination" in f.lower() for f in result.hard_failures)
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.REJECTED

    def test_void_policy_basis_divergence_is_risk_flag(self) -> None:
        # Proven fair_value vs resolves_to_other: identical in normal
        # resolution, divergent only in the cancellation tail -> risk flag.
        result = validate_rules(
            kalshi_facts(
                resolution_text=KALSHI_FAIR_VALUE_CANCELLATION,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
            poly_facts(
                resolution_text=POLY_RESOLVES_TO_OTHER,
                determination_time=None,
                resolution_source="",
                void_policy=None,
            ),
        )
        assert result.hard_failures == ()
        assert any("void" in f.lower() for f in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_known_void_policy_difference_is_risk_flag(self) -> None:
        result = validate_rules(
            kalshi_facts(void_policy="trades_stand"),
            poly_facts(void_policy="refund_on_postponement"),
        )
        assert result.hard_failures == ()
        assert any("void" in f.lower() for f in result.risk_flags)
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_mid_similarity_still_manual_review(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.75, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_low_similarity_still_rejected(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.2, rules=result) is MatchStatus.REJECTED


class TestDecideStatus:
    def test_high_confidence_clean_rules_accepted(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED

    def test_hard_failure_rejected_even_at_high_similarity(self) -> None:
        # A material (>7d) determination-horizon gap is a different-event hard
        # failure; a small gap is only a risk flag.
        result = validate_rules(
            kalshi_facts(), poly_facts(determination_time=T0 + timedelta(days=30))
        )
        assert decide_status(similarity_score=0.99, rules=result) is MatchStatus.REJECTED

    def test_low_similarity_rejected(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.2, rules=result) is MatchStatus.REJECTED

    def test_mid_similarity_goes_to_manual_review(self) -> None:
        result = validate_rules(kalshi_facts(), poly_facts())
        assert decide_status(similarity_score=0.75, rules=result) is MatchStatus.MANUAL_REVIEW

    def test_many_risk_flags_still_accept(self) -> None:
        # Settlement-mechanic caveats no longer cap acceptance; they ride along
        # as risk_flags so a human can verify them before trading.
        result = validate_rules(
            kalshi_facts(is_sports=True, can_close_early=True, resolution_source=""),
            poly_facts(
                is_sports=True,
                game_start_time=T0 - timedelta(hours=3),
                resolution_source="",
            ),
        )
        assert len(result.risk_flags) >= 3
        assert result.hard_failures == ()
        assert decide_status(similarity_score=0.95, rules=result) is MatchStatus.ACCEPTED
