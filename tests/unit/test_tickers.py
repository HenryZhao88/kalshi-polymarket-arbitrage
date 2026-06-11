"""Conservative Kalshi ticker inference tests."""

from arb_scanner.app.markets.parsers import MarketType
from arb_scanner.app.markets.tickers import parse_kalshi_ticker


def test_crypto_ticker_extracts_date_and_threshold() -> None:
    inferred = parse_kalshi_ticker("KXBTCD-26JUN30-T70000")
    assert inferred.event_date is not None
    assert inferred.event_date.value == "2026-06-30"
    assert inferred.threshold is not None
    assert inferred.threshold.value == "70000"
    assert inferred.category_family is not None
    assert inferred.category_family.value == "crypto"
    assert inferred.market_type is not None
    assert inferred.market_type.value == MarketType.CRYPTO_PRICE_THRESHOLD.value
    assert inferred.threshold.source == "ticker"


def test_election_ticker_extracts_state_office_and_year() -> None:
    inferred = parse_kalshi_ticker("SENATESC-28-R")
    assert inferred.event_year is not None and inferred.event_year.value == "2028"
    assert inferred.state is not None and inferred.state.value == "SC"
    assert inferred.office is not None and inferred.office.value == "senate"
    assert inferred.market_type is not None
    assert inferred.market_type.value == MarketType.SENATE_WINNER.value


def test_nominee_ticker_is_not_inferred_as_governor_winner() -> None:
    inferred = parse_kalshi_ticker("KXGOVSCNOMR-26-AWIL")
    assert inferred.state is not None and inferred.state.value == "SC"
    assert inferred.office is not None and inferred.office.value == "governor"
    assert inferred.market_type is not None
    assert inferred.market_type.value == MarketType.PARTY_NOMINEE.value


def test_unknown_ticker_does_not_invent_fields() -> None:
    inferred = parse_kalshi_ticker("KXUNKNOWN")
    assert inferred.event_date is None
    assert inferred.market_type is None
    assert inferred.threshold is None


def test_sports_ticker_supplies_conflict_only_market_type() -> None:
    inferred = parse_kalshi_ticker("KXMLSCUP-26-NYC")
    assert inferred.category_family is not None
    assert inferred.category_family.value == "sports"
    assert inferred.market_type is not None
    assert inferred.market_type.value == MarketType.SPORTS_MONEYLINE.value
