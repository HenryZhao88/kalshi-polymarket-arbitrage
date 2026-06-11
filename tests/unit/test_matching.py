"""Title normalization, feature parsing, and similarity-cascade tests."""

from datetime import UTC, datetime
from decimal import Decimal

from arb_scanner.app.markets.matching import MatchStage, similarity
from arb_scanner.app.markets.parsers import normalize_title, parse_features

D = Decimal


class TestNormalizeTitle:
    def test_lowercases_and_strips_noise(self) -> None:
        assert normalize_title("Will Bitcoin reach $70,000 by Dec 31?") == (
            "bitcoin reach 70000 dec 31"
        )

    def test_strips_question_prefixes_and_punctuation(self) -> None:
        assert normalize_title("Will the Lakers beat the Celtics?") == "lakers beat celtics"

    def test_normalizes_thousands_shorthand(self) -> None:
        assert normalize_title("BTC above $70k?") == "btc above 70000"

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("  Fed   cuts rates ") == "fed cuts rates"


class TestParseFeatures:
    def test_extracts_strike(self) -> None:
        features = parse_features("Bitcoin above $70,000 on June 30?")
        assert features.strike == D("70000")

    def test_extracts_decimal_strike(self) -> None:
        features = parse_features("Will CPI be above 3.5%?")
        assert features.strike == D("3.5")

    def test_no_strike(self) -> None:
        assert parse_features("Will the Lakers win the title?").strike is None

    def test_extracts_direction(self) -> None:
        assert parse_features("BTC above $70k").direction == "above"
        assert parse_features("ETH below $2,000").direction == "below"

    def test_extracts_entity_tokens(self) -> None:
        features = parse_features("Will Bitcoin reach $70,000 by Dec 31?")
        assert "bitcoin" in features.tokens


class TestSimilarityCascade:
    def test_exact_structured_match(self) -> None:
        time = datetime(2026, 6, 30, tzinfo=UTC)
        result = similarity(
            "Bitcoin above $70,000 on June 30?",
            "Will BTC be above $70k on June 30?",
            determination_time_a=time,
            determination_time_b=time,
        )
        # same strike + direction + time: structured agreement boosts score
        assert result.stage in (MatchStage.STRUCTURED, MatchStage.FUZZY)
        assert result.score >= 0.7

    def test_identical_titles_score_high(self) -> None:
        result = similarity("Lakers beat Celtics", "Lakers beat Celtics")
        assert result.score >= 0.95

    def test_unrelated_titles_score_low(self) -> None:
        result = similarity("Will it rain in NYC tomorrow?", "Fed cuts rates in September?")
        assert result.score < 0.4

    def test_same_entity_different_strike_not_structured(self) -> None:
        result = similarity("Bitcoin above $70,000?", "Bitcoin above $80,000?")
        assert result.stage is not MatchStage.STRUCTURED
        assert result.structured_conflicts  # strike mismatch recorded

    def test_token_overlap_fallback(self) -> None:
        result = similarity("lakers celtics game 7 winner", "celtics lakers game seven")
        assert result.score > 0.4
