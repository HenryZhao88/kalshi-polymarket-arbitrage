"""Kalshi fee engine tests.

Coarse-schedule vectors derive from the published formulas
(https://kalshi.com/fee-schedule). Fill-exact vectors reproduce the three worked
examples on https://docs.kalshi.com/getting_started/fee_rounding verbatim
(retrieved 2026-06-11).
"""

from decimal import Decimal

import pytest

from arb_scanner.app.fees.kalshi import (
    KalshiFillFeeAccumulator,
    debit_deposit_fee,
    kalshi_fee_raw,
    kalshi_maker_fee,
    kalshi_taker_fee,
)
from arb_scanner.app.types import Money

D = Decimal


class TestCoarseTakerFee:
    def test_100_contracts_at_50c_is_max_fee(self) -> None:
        # 0.07 × 100 × 0.5 × 0.5 = $1.75 exactly
        assert kalshi_taker_fee(100, D("0.50")) == Money.from_dollars("1.75")

    def test_rounds_up_to_cent(self) -> None:
        # 0.07 × 1 × 0.5 × 0.5 = 0.0175 → $0.02
        assert kalshi_taker_fee(1, D("0.50")) == Money.from_dollars("0.02")

    def test_price_near_zero(self) -> None:
        # 0.07 × 1 × 0.01 × 0.99 = 0.000693 → $0.01
        assert kalshi_taker_fee(1, D("0.01")) == Money.from_dollars("0.01")

    def test_price_near_one(self) -> None:
        # symmetric with near-zero
        assert kalshi_taker_fee(1, D("0.99")) == Money.from_dollars("0.01")

    def test_large_size(self) -> None:
        # 0.07 × 100_000 × 0.61 × 0.39 = 1665.30 exactly
        assert kalshi_taker_fee(100_000, D("0.61")) == Money.from_dollars("1665.30")

    def test_multiplier_zero_means_fee_free_series(self) -> None:
        assert kalshi_taker_fee(100, D("0.50"), multiplier=D(0)) == Money.zero()

    def test_multiplier_scales_coefficient(self) -> None:
        # half schedule: 0.5 × 0.07 × 100 × 0.25 = 0.875 → $0.88
        assert kalshi_taker_fee(100, D("0.50"), multiplier=D("0.5")) == Money.from_dollars("0.88")

    def test_zero_contracts(self) -> None:
        assert kalshi_taker_fee(0, D("0.50")) == Money.zero()

    def test_rejects_price_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            kalshi_taker_fee(1, D("1.5"))


class TestCoarseMakerFee:
    def test_maker_coefficient(self) -> None:
        # 0.0175 × 100 × 0.5 × 0.5 = 0.4375 → $0.44
        assert kalshi_maker_fee(100, D("0.50")) == Money.from_dollars("0.44")

    def test_small_maker_fee_still_rounds_up(self) -> None:
        # 0.0175 × 1 × 0.5 × 0.5 = 0.004375 → $0.01
        assert kalshi_maker_fee(1, D("0.50")) == Money.from_dollars("0.01")


class TestRawFee:
    def test_raw_is_unrounded(self) -> None:
        assert kalshi_fee_raw(D(1), D("0.50"), D("0.07")) == D("0.0175")


class TestFillExactModel:
    """The three verbatim worked examples from docs.kalshi.com fee_rounding."""

    def test_example_1_subpenny_prices(self) -> None:
        # Buy 3 contracts at $0.055 as three 1-lot fills; trade fee $0.0085/fill.
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.01"))
        cost = Money.from_dollars("0.055")
        fee = D("0.0085")

        f1 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f1.trade_fee == Money.from_dollars("0.0085")
        assert f1.rounding_fee == Money.from_dollars("0.0065")
        assert f1.rebate == Money.zero()
        assert f1.net_fee == Money.from_dollars("0.0150")
        assert acc.accumulated == Money.from_dollars("0.0065")

        f2 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f2.rounding_fee == Money.from_dollars("0.0065")
        assert f2.rebate == Money.from_dollars("0.01")
        assert f2.net_fee == Money.from_dollars("0.0050")
        assert acc.accumulated == Money.from_dollars("0.0030")

        f3 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f3.rebate == Money.zero()
        assert f3.net_fee == Money.from_dollars("0.0150")
        assert acc.accumulated == Money.from_dollars("0.0095")

    def test_example_2_fractional_contracts(self) -> None:
        # Buy 0.90 contracts at $0.50 as three 0.30-lot fills; trade fee $0.0041/fill.
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.01"))
        cost = Money.from_dollars("0.15")
        fee = D("0.0041")

        f1 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f1.rounding_fee == Money.from_dollars("0.0059")
        assert f1.net_fee == Money.from_dollars("0.0100")

        f2 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f2.rebate == Money.from_dollars("0.01")
        assert f2.net_fee == Money.zero()

        f3 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f3.net_fee == Money.from_dollars("0.0100")
        assert acc.accumulated == Money.from_dollars("0.0077")

    def test_example_3_fractional_and_subpenny(self) -> None:
        # Buy 0.09 contracts at $0.3301 as three 0.03-lot fills; trade fee $0.0005/fill.
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.01"))
        cost = Money.from_dollars("0.009903")
        fee = D("0.0005")

        f1 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f1.rounding_fee == Money.from_dollars("0.009597")
        assert f1.net_fee == Money.from_dollars("0.010097")
        assert acc.accumulated == Money.from_dollars("0.009597")

        f2 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f2.rebate == Money.from_dollars("0.01")
        assert f2.net_fee == Money.from_dollars("0.000097")

        f3 = acc.apply_fill(notional=cost, trade_fee_raw=fee)
        assert f3.rebate == Money.from_dollars("0.01")
        assert f3.net_fee == Money.from_dollars("0.000097")

    def test_trade_fee_rounds_up_to_centicent(self) -> None:
        # raw 0.00363825 → $0.0037
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.01"))
        f = acc.apply_fill(notional=Money.from_dollars("0.055"), trade_fee_raw=D("0.00363825"))
        assert f.trade_fee == Money.from_dollars("0.0037")

    def test_net_fee_never_negative(self) -> None:
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.01"))
        for _ in range(50):
            f = acc.apply_fill(notional=Money.from_dollars("0.10"), trade_fee_raw=D("0.0001"))
            assert f.net_fee >= Money.zero()

    def test_direct_member_precision_no_rounding_fee(self) -> None:
        # Direct members: $0.0001 precision; an aligned fill produces no rounding fee.
        acc = KalshiFillFeeAccumulator(balance_precision=D("0.0001"))
        f = acc.apply_fill(notional=Money.from_dollars("0.0550"), trade_fee_raw=D("0.0037"))
        assert f.rounding_fee == Money.zero()
        assert f.net_fee == Money.from_dollars("0.0037")


class TestFunding:
    def test_debit_deposit_fee_two_percent(self) -> None:
        assert debit_deposit_fee(Money.from_dollars("100")) == Money.from_dollars("2.00")

    def test_debit_deposit_fee_rounds_up(self) -> None:
        # 2% of $0.30 = $0.006 → $0.01 (conservative ceil)
        assert debit_deposit_fee(Money.from_dollars("0.30")) == Money.from_dollars("0.01")

    def test_configurable_rate(self) -> None:
        assert debit_deposit_fee(Money.from_dollars("100"), rate=D("0.01")) == Money.from_dollars(
            "1.00"
        )
