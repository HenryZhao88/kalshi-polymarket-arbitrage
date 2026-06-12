"""Title normalization, feature parsing, and similarity-cascade tests."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from arb_scanner.app.markets.matching import MatchStage, similarity
from arb_scanner.app.markets.parsers import MarketType, normalize_title, parse_features

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

    def test_extracts_explicit_date_and_year(self) -> None:
        features = parse_features("Will turnout exceed 60% before November 3, 2026?")
        assert features.event_date is not None
        assert features.event_date.isoformat() == "2026-11-03"
        assert features.event_year == 2026
        assert features.strike == D("60")

    def test_extracts_sports_line(self) -> None:
        features = parse_features("Will Boston cover a spread of -3.5?", category="sports")
        assert features.market_type is MarketType.SPORTS_SPREAD
        assert features.strike == D("-3.5")

    def test_description_can_supply_structured_evidence(self) -> None:
        features = parse_features(
            "Will rainfall set a record?",
            description="Resolves yes if rainfall exceeds 5 inches by December 31, 2026.",
            category="weather",
        )
        assert features.market_type is MarketType.WEATHER_THRESHOLD
        assert features.event_date is not None
        assert features.event_date_evidence is not None
        assert features.event_date_evidence.source == "description"

    @pytest.mark.parametrize(
        ("title", "category", "expected"),
        [
            ("Will Alice win the election?", None, MarketType.ELECTION_WINNER),
            ("Will Alice be the party nominee?", None, MarketType.PARTY_NOMINEE),
            (
                "Will Alice finish 2nd in the gubernatorial primary?",
                None,
                MarketType.PRIMARY_PLACEMENT,
            ),
            (
                "Will Alice qualify for the primary runoff?",
                None,
                MarketType.PRIMARY_ADVANCEMENT,
            ),
            ("Will Alice win the governor race?", None, MarketType.GOVERNOR_WINNER),
            ("Will Alice win the Senate race?", None, MarketType.SENATE_WINNER),
            ("Will Alice win the House race?", None, MarketType.HOUSE_WINNER),
            ("Will Alice win the presidential election?", None, MarketType.PRESIDENTIAL_WINNER),
            ("Will Republicans control the Senate?", None, MarketType.PARTY_CONTROL),
            ("Will the margin of victory exceed 5 points?", None, MarketType.MARGIN_SPREAD),
            ("Will Alice receive exactly 51% vote share?", None, MarketType.EXACT_VOTE_SHARE),
            ("Will voter turnout exceed 60%?", None, MarketType.TURNOUT),
            ("Will the total vote count exceed 2,000,000?", None, MarketType.TURNOUT),
            ("Will Alice be confirmed to the office?", None, MarketType.OFFICE_HOLDER),
            ("Will Boston beat New York?", "sports", MarketType.SPORTS_MONEYLINE),
            ("Will Rhode Island FC win the USL Championship?", None, MarketType.SPORTS_MONEYLINE),
            ("Will Boston cover the spread?", "sports", MarketType.SPORTS_SPREAD),
            ("Will total points be over 48.5?", "sports", MarketType.SPORTS_TOTAL),
            ("Will Bitcoin exceed $70,000?", None, MarketType.CRYPTO_PRICE_THRESHOLD),
            ("Will the Nasdaq exceed 20,000?", None, MarketType.STOCK_INDEX_PRICE_THRESHOLD),
            ("Will temperature exceed 90 degrees?", None, MarketType.WEATHER_THRESHOLD),
            (
                "Will at least 2 teams from South America reach the knockout stage "
                "of the 2026 Men's FIFA World Cup?",
                None,
                MarketType.SPORTS_STAGE_COUNT,
            ),
            (
                "Will exactly 3 teams from Europe reach the knockout stage?",
                "sports",
                MarketType.SPORTS_STAGE_COUNT,
            ),
            (
                "Will October be the best month for Bitcoin in 2026?",
                None,
                MarketType.CRYPTO_MONTHLY_PERFORMANCE,
            ),
            (
                "Which calendar month will see Bitcoin's highest percentage change?",
                None,
                MarketType.CRYPTO_MONTHLY_PERFORMANCE,
            ),
            # Threshold-by-date phrasing must keep its existing classification.
            (
                "Will Bitcoin be above $100000 by October 1, 2026 at 12:00AM ET?",
                None,
                MarketType.CRYPTO_PRICE_THRESHOLD,
            ),
            (
                "Will South America (CONMEBOL) win the 2026 Men's World Cup?",
                None,
                MarketType.SPORTS_WC_CONTINENT_WINNER,
            ),
            (
                "Will the winner of the 2026 Men's FIFA World Cup be from any "
                "continent other than Europe or South America?",
                None,
                MarketType.SPORTS_WC_CONTINENT_WINNER,
            ),
            # Country-level winner stays moneyline (different bet from continent).
            ("Will Brazil win the 2026 World Cup?", "sports", MarketType.SPORTS_MONEYLINE),
            (
                "Will the listed Democratic Senate candidates all win their "
                "primary elections?",
                None,
                MarketType.ELECTION_CANDIDATE_SWEEP,
            ),
            (
                "Will Democratic Senate incumbents win all their nominating "
                "elections in the 2026 cycle?",
                None,
                MarketType.ELECTION_CANDIDATE_SWEEP,
            ),
            # Single race winner is never a sweep.
            ("Will Alice win the Senate race?", None, MarketType.SENATE_WINNER),
        ],
    )
    def test_market_type_taxonomy(
        self, title: str, category: str | None, expected: MarketType
    ) -> None:
        assert parse_features(title, category=category).market_type is expected


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
