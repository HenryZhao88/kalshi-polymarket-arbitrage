"""Bridge cost model, slippage models, and profit composition tests."""

from decimal import Decimal

import pytest

from arb_scanner.app.fees.bridge import BridgeQuote
from arb_scanner.app.fees.profit import (
    FeeBreakdown,
    annualized_return,
    capital_locked,
    gross_profit,
    net_profit,
    simple_return,
)
from arb_scanner.app.fees.slippage import (
    DepthImpactSlippage,
    EdgeFractionSlippage,
    FixedCentsSlippage,
)
from arb_scanner.app.types import BookLevel, Money

D = Decimal

# Verbatim example response from
# https://docs.polymarket.com/api-reference/bridge/get-a-quote (retrieved 2026-06-11)
BRIDGE_PAYLOAD = {
    "estCheckoutTimeMs": 25000,
    "estFeeBreakdown": {
        "appFeeLabel": "Fun.xyz fee",
        "appFeePercent": 0,
        "appFeeUsd": 0,
        "fillCostPercent": 0,
        "fillCostUsd": 0,
        "gasUsd": 0.003854,
        "maxSlippage": 0,
        "minReceived": 14.488305,
        "swapImpact": 0,
        "swapImpactUsd": 0,
        "totalImpact": 0,
        "totalImpactUsd": 0,
    },
    "estInputUsd": 14.488305,
    "estOutputUsd": 14.488305,
    "estToTokenBaseUnit": "14491203",
    "quoteId": "0x00c34ba467184b0146406d62b0e60aaa24ed52460bd456222b6155a0d9de0ad5",
}


class TestBridgeQuote:
    def test_parses_doc_example(self) -> None:
        quote = BridgeQuote.from_payload(BRIDGE_PAYLOAD)
        assert quote.gas == Money.from_dollars("0.003854")
        assert quote.app_fee == Money.zero()
        assert quote.fill_cost == Money.zero()
        assert quote.swap_impact == Money.zero()
        assert quote.quote_id.startswith("0x00c34ba4")

    def test_total_cost_sums_components(self) -> None:
        quote = BridgeQuote.from_payload(BRIDGE_PAYLOAD)
        assert quote.total_cost == Money.from_dollars("0.003854")

    def test_never_constructed_without_live_payload(self) -> None:
        # No default constructor path: a payload missing the fee breakdown must fail.
        with pytest.raises(KeyError):
            BridgeQuote.from_payload({"estInputUsd": 1})


class TestSlippageModels:
    def test_fixed_cents_per_share(self) -> None:
        model = FixedCentsSlippage(cents_per_share=D("0.5"))
        assert model.estimate(size=1000, quoted_edge=Money.from_dollars("30")) == (
            Money.from_dollars("5.00")
        )

    def test_edge_fraction(self) -> None:
        model = EdgeFractionSlippage(fraction=D("0.10"))
        assert model.estimate(size=1000, quoted_edge=Money.from_dollars("30")) == (
            Money.from_dollars("3.00")
        )

    def test_depth_impact_vwap_vs_top(self) -> None:
        # 100 @ 0.40 then 200 @ 0.42: filling 300 costs 124.00 vs 120.00 at top
        levels = [
            BookLevel(price=D("0.40"), size=D(100)),
            BookLevel(price=D("0.42"), size=D(200)),
        ]
        model = DepthImpactSlippage(levels=levels)
        assert model.estimate(size=300, quoted_edge=Money.zero()) == Money.from_dollars("4.00")

    def test_depth_impact_insufficient_depth_raises(self) -> None:
        levels = [BookLevel(price=D("0.40"), size=D(100))]
        model = DepthImpactSlippage(levels=levels)
        with pytest.raises(ValueError, match="depth"):
            model.estimate(size=200, quoted_edge=Money.zero())


class TestProfitComposition:
    def test_gross_profit(self) -> None:
        # size × (1 − p1 − p2): spec reference scenario 1 legs
        assert gross_profit(100, D("0.90"), D("0.03")) == Money.from_dollars("7.00")

    def test_gross_can_be_negative(self) -> None:
        assert gross_profit(100, D("0.52"), D("0.49")) == Money.from_dollars("-1.00")

    def test_net_profit_subtracts_every_component(self) -> None:
        fees = FeeBreakdown(
            kalshi_fee=Money.from_dollars("0.63"),
            polymarket_fee=Money.from_dollars("0.1164"),
            bridge_cost=Money.from_dollars("0.01"),
            withdrawal_cost=Money.from_dollars("0.02"),
            gas_cost=Money.from_dollars("0.03"),
            processor_cost=Money.from_dollars("0.04"),
            conversion_cost=Money.from_dollars("0.05"),
            expected_slippage=Money.from_dollars("0.06"),
            unknown_cost_buffer=Money.from_dollars("0.07"),
        )
        net = net_profit(gross=Money.from_dollars("7.00"), fees=fees)
        assert net == Money.from_dollars("5.9736")

    def test_fee_breakdown_total_excludes_rebates(self) -> None:
        fees = FeeBreakdown(
            kalshi_fee=Money.from_dollars("1.00"),
            optional_rebates=Money.from_dollars("0.50"),
        )
        assert fees.total == Money.from_dollars("1.00")

    def test_capital_locked(self) -> None:
        locked = capital_locked(100, D("0.90"), D("0.03"), fee_buffer=Money.from_dollars("1"))
        assert locked == Money.from_dollars("94.00")

    def test_simple_and_annualized_return(self) -> None:
        ret = simple_return(net=Money.from_dollars("6.25"), locked=Money.from_dollars("94"))
        assert ret.quantize(D("0.0001")) == D("0.0665")
        annual = annualized_return(ret, hold_days=D("30"))
        assert annual.quantize(D("0.001")) == D("0.809")

    def test_zero_locked_capital_raises(self) -> None:
        with pytest.raises(ValueError, match="locked"):
            simple_return(net=Money.from_dollars("1"), locked=Money.zero())
