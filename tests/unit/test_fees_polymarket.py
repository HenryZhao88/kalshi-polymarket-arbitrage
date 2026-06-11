"""Polymarket fee engine tests.

Generalized-form vectors come from the official SDK test suite
(Polymarket/py-clob-client-v2 tests/test_fee_calculations.py, fetched 2026-06-11):
rate=0.25, exponent=2, 100 contracts. Category rates and rounding rules from
https://docs.polymarket.com/trading/fees (retrieved 2026-06-11).
"""

from decimal import Decimal

import pytest

from arb_scanner.app.fees.polymarket import (
    CATEGORY_TAKER_RATES,
    FeeRateSource,
    FeeSchedule,
    fee_schedule_from_metadata,
    maker_rebate,
    polymarket_taker_fee,
    polymarket_taker_fee_raw,
    resolve_fee_schedule,
)
from arb_scanner.app.types import Money

D = Decimal


class TestGeneralizedFormulaSdkVectors:
    """fee = shares × rate × (p(1−p))^exponent — SDK test vectors, rate=0.25, e=2."""

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.5", "1.5625"),
            ("0.3", "1.1025"),
            ("0.1", "0.2025"),
            ("0.05", "0.05640625"),
            ("0.01", "0.00245025"),
            ("0.7", "1.1025"),  # symmetric with 0.3
            ("0.9", "0.2025"),  # symmetric with 0.1
        ],
    )
    def test_sdk_vector(self, price: str, expected: str) -> None:
        raw = polymarket_taker_fee_raw(D(100), D(price), rate=D("0.25"), exponent=D(2))
        assert raw == D(expected)


class TestSpecFormulaExponentOne:
    def test_docs_formula_is_exponent_one(self) -> None:
        # fee = C × feeRate × p × (1−p): 100 × 0.04 × 0.03 × 0.97 = 0.1164
        raw = polymarket_taker_fee_raw(D(100), D("0.03"), rate=D("0.04"), exponent=D(1))
        assert raw == D("0.1164")


class TestRounding:
    def test_rounded_to_five_decimals(self) -> None:
        # 0.05640625 → 0.05641 (half-up at 5 dp)
        fee = polymarket_taker_fee(D(100), D("0.05"), rate=D("0.25"), exponent=D(2))
        assert fee == Money.from_dollars("0.05641")

    def test_below_minimum_rounds_to_zero(self) -> None:
        # 1 share × 0.04 × 0.0001 × 0.9999 ≈ 0.0000039996 → rounds to 0
        fee = polymarket_taker_fee(D(1), D("0.0001"), rate=D("0.04"), exponent=D(1))
        assert fee == Money.zero()

    def test_minimum_chargeable_fee(self) -> None:
        # smallest charged fee is 0.00001 USDC
        fee = polymarket_taker_fee(D(1), D("0.0004"), rate=D("0.04"), exponent=D(1))
        assert fee == Money.from_dollars("0.00002")

    def test_zero_rate_is_free(self) -> None:
        fee = polymarket_taker_fee(D(1000), D("0.5"), rate=D(0), exponent=D(1))
        assert fee == Money.zero()

    def test_large_size(self) -> None:
        # 10_000 × 0.04 × 0.46 × 0.54 = 99.36
        fee = polymarket_taker_fee(D(10_000), D("0.46"), rate=D("0.04"), exponent=D(1))
        assert fee == Money.from_dollars("99.36")

    def test_rejects_price_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            polymarket_taker_fee(D(1), D("1.01"), rate=D("0.04"), exponent=D(1))


class TestCategoryRates:
    def test_official_schedule(self) -> None:
        assert CATEGORY_TAKER_RATES["crypto"] == D("0.07")
        assert CATEGORY_TAKER_RATES["sports"] == D("0.03")
        assert CATEGORY_TAKER_RATES["finance"] == D("0.04")
        assert CATEGORY_TAKER_RATES["politics"] == D("0.04")
        assert CATEGORY_TAKER_RATES["mentions"] == D("0.04")
        assert CATEGORY_TAKER_RATES["tech"] == D("0.04")
        assert CATEGORY_TAKER_RATES["economics"] == D("0.05")
        assert CATEGORY_TAKER_RATES["culture"] == D("0.05")
        assert CATEGORY_TAKER_RATES["weather"] == D("0.05")
        assert CATEGORY_TAKER_RATES["other"] == D("0.05")
        assert CATEGORY_TAKER_RATES["geopolitics"] == D(0)


class TestFeeResolution:
    """Resolution order per VERIFICATION.md §2.2: market metadata wins, category
    defaults are a flagged fallback, never base_fee alone."""

    def test_market_metadata_wins(self) -> None:
        market = FeeSchedule(rate=D("0.07"), exponent=D(1), source=FeeRateSource.MARKET_METADATA)
        resolved = resolve_fee_schedule(market_schedule=market, category="sports")
        assert resolved.rate == D("0.07")
        assert resolved.source is FeeRateSource.MARKET_METADATA

    def test_gamma_fee_schedule_is_parsed(self) -> None:
        schedule = fee_schedule_from_metadata(
            {"feeSchedule": {"rate": "0.05", "exponent": 1, "takerOnly": True}}
        )
        assert schedule is not None
        assert schedule.rate == D("0.05")
        assert schedule.exponent == D(1)
        assert schedule.source is FeeRateSource.MARKET_METADATA

    def test_compact_clob_fee_schedule_is_parsed(self) -> None:
        schedule = fee_schedule_from_metadata({"fd": {"r": "0.05", "e": "1", "to": True}})
        assert schedule is not None
        assert schedule.rate == D("0.05")

    def test_category_fallback_is_flagged(self) -> None:
        resolved = resolve_fee_schedule(market_schedule=None, category="sports")
        assert resolved.rate == D("0.03")
        assert resolved.exponent == D(1)
        assert resolved.source is FeeRateSource.CATEGORY_DEFAULT

    def test_unknown_category_uses_other_rate(self) -> None:
        resolved = resolve_fee_schedule(market_schedule=None, category="something-new")
        assert resolved.rate == D("0.05")
        assert resolved.source is FeeRateSource.CATEGORY_DEFAULT

    def test_no_information_is_unpriceable(self) -> None:
        resolved = resolve_fee_schedule(market_schedule=None, category=None)
        assert resolved.source is FeeRateSource.UNKNOWN


class TestRebates:
    def test_rebate_is_separate_and_optional(self) -> None:
        # 25% of $1.00 collected taker fees
        assert maker_rebate(Money.from_dollars("1.00"), D("0.25")) == Money.from_dollars("0.25")

    def test_zero_rate(self) -> None:
        assert maker_rebate(Money.from_dollars("1.00"), D(0)) == Money.zero()
