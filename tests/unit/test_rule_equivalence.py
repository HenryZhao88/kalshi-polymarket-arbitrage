"""Rule-equivalence validation tests (matching pipeline stages 4–5).

Covers the SPEC trap cases: same title but different determination time; same game
but different void rules; sports early-start divergence; UMA challenge windows.
"""

from datetime import UTC, datetime, timedelta

from arb_scanner.app.markets.rule_equivalence import (
    KalshiRuleFacts,
    MatchStatus,
    PolymarketRuleFacts,
    decide_status,
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
