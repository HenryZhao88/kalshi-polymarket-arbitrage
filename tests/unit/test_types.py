"""Unit tests for shared domain types, primarily Money arithmetic and rounding."""

from decimal import Decimal

import pytest

from arb_scanner.app.types import BookLevel, Money, Side, Venue


class TestMoneyConstruction:
    def test_from_dollars_str(self) -> None:
        assert Money.from_dollars("1.25").micros == 1_250_000

    def test_from_dollars_decimal(self) -> None:
        assert Money.from_dollars(Decimal("0.00001")).micros == 10

    def test_from_cents(self) -> None:
        assert Money.from_cents(7).micros == 70_000

    def test_zero(self) -> None:
        assert Money.zero().micros == 0

    def test_sub_cent_precision_preserved(self) -> None:
        # Polymarket minimum fee is $0.00001; Kalshi fill-exact rounds to $0.0001.
        assert Money.from_dollars("0.0001").micros == 100

    def test_rejects_precision_below_micro(self) -> None:
        with pytest.raises(ValueError, match="micro"):
            Money.from_dollars("0.0000001")


class TestMoneyArithmetic:
    def test_add(self) -> None:
        assert Money.from_dollars("1.10") + Money.from_dollars("0.15") == Money.from_dollars(
            "1.25"
        )

    def test_sub_can_go_negative(self) -> None:
        assert (Money.from_dollars("1") - Money.from_dollars("2.50")).to_dollars() == Decimal(
            "-1.5"
        )

    def test_mul_int(self) -> None:
        assert Money.from_cents(3) * 100 == Money.from_dollars("3.00")

    def test_comparisons(self) -> None:
        assert Money.from_cents(1) < Money.from_cents(2) <= Money.from_cents(2)

    def test_to_dollars_round_trip(self) -> None:
        assert Money.from_dollars("123.456789").to_dollars() == Decimal("123.456789")


class TestMoneyRounding:
    def test_ceil_to_cent_rounds_up(self) -> None:
        assert Money.from_dollars("0.0701").ceil_to_cent() == Money.from_dollars("0.08")

    def test_ceil_to_cent_exact_cent_unchanged(self) -> None:
        assert Money.from_dollars("0.07").ceil_to_cent() == Money.from_dollars("0.07")

    def test_ceil_to_cent_negative_rounds_toward_zero_up(self) -> None:
        # ceiling of -0.071 in cents is -0.07
        assert Money.from_dollars("-0.071").ceil_to_cent() == Money.from_dollars("-0.07")

    def test_ceil_to_quantum_tenth_of_cent(self) -> None:
        # Kalshi fill-exact: round trade fee UP to nearest $0.0001
        assert Money.from_dollars("0.00011").ceil_to(Decimal("0.0001")) == Money.from_dollars(
            "0.0002"
        )

    def test_ceil_to_quantum_exact_unchanged(self) -> None:
        assert Money.from_dollars("0.0002").ceil_to(Decimal("0.0001")) == Money.from_dollars(
            "0.0002"
        )


class TestBookLevel:
    def test_price_is_probability(self) -> None:
        lvl = BookLevel(price=Decimal("0.61"), size=250)
        assert lvl.price == Decimal("0.61")
        assert lvl.size == 250

    def test_rejects_price_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            BookLevel(price=Decimal("1.5"), size=10)

    def test_rejects_negative_size(self) -> None:
        with pytest.raises(ValueError):
            BookLevel(price=Decimal("0.5"), size=-1)


def test_side_complement() -> None:
    assert Side.YES.complement is Side.NO
    assert Side.NO.complement is Side.YES


def test_venue_values() -> None:
    assert Venue.KALSHI.value == "kalshi"
    assert Venue.POLYMARKET.value == "polymarket"
