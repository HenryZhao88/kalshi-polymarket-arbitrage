"""Execution adapter tests (no network): Kalshi adapter over a fake REST client,
Polymarket adapter fail-closed + test-double round trip."""

from decimal import Decimal
from typing import Any

import pytest

from arb_scanner.app.execution.adapters import (
    KalshiExecutionAdapter,
    OrderStatus,
    PolymarketExecutionAdapter,
    PolymarketExecutionUnavailable,
    _price_to_cents,
)
from arb_scanner.app.types import Money, Side, Venue


class FakeKalshiClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self.balance_cents = 250_000

    async def get_balance(self) -> dict[str, Any]:
        return {"balance": self.balance_cents}

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {
            "order": {
                "order_id": "ord-1",
                "status": "executed",
                "filled_count": kwargs["count"],
                "average_fill_price": kwargs.get("yes_price_cents") or kwargs.get("no_price_cents"),
            }
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.canceled.append(order_id)
        return {"order": {"order_id": order_id, "status": "canceled"}}


class TestPriceConversion:
    def test_rounds_and_clamps_to_cents(self) -> None:
        assert _price_to_cents(Decimal("0.425")) == 43  # half-up
        assert _price_to_cents(Decimal("0.001")) == 1  # clamp floor
        assert _price_to_cents(Decimal("0.999")) == 99  # clamp ceil


class TestKalshiAdapter:
    async def test_balance_is_converted_from_cents(self) -> None:
        adapter = KalshiExecutionAdapter(FakeKalshiClient())  # type: ignore[arg-type]
        balance = await adapter.available_balance()
        assert balance == Money.from_dollars("2500")

    async def test_buy_yes_sends_yes_price_and_normalizes_fill(self) -> None:
        client = FakeKalshiClient()
        adapter = KalshiExecutionAdapter(client)  # type: ignore[arg-type]
        result = await adapter.place_buy(
            market_id="KXBTC-T70000",
            side=Side.YES,
            size=10,
            limit_price=Decimal("0.42"),
            client_order_id="cid-1",
        )
        assert client.created[0]["yes_price_cents"] == 42
        assert client.created[0]["action"] == "buy"
        assert result.venue is Venue.KALSHI
        assert result.status is OrderStatus.FILLED
        assert result.fully_filled
        assert result.filled_size == 10
        assert result.avg_price == Decimal("0.42")

    async def test_buy_no_sends_no_price(self) -> None:
        client = FakeKalshiClient()
        adapter = KalshiExecutionAdapter(client)  # type: ignore[arg-type]
        await adapter.place_buy(
            market_id="KXBTC-T70000", side=Side.NO, size=5, limit_price=Decimal("0.58")
        )
        assert client.created[0]["no_price_cents"] == 58
        assert "yes_price_cents" not in client.created[0]

    async def test_cancel_delegates(self) -> None:
        client = FakeKalshiClient()
        adapter = KalshiExecutionAdapter(client)  # type: ignore[arg-type]
        await adapter.cancel("ord-9")
        assert client.canceled == ["ord-9"]


class TestPolymarketAdapter:
    async def test_fails_closed_without_signing_client(self) -> None:
        adapter = PolymarketExecutionAdapter(signing_client=None)
        with pytest.raises(PolymarketExecutionUnavailable):
            await adapter.place_buy(
                market_id="0xtoken", side=Side.YES, size=10, limit_price=Decimal("0.40")
            )
        with pytest.raises(PolymarketExecutionUnavailable):
            await adapter.available_balance()

    async def test_place_order_via_test_double(self) -> None:
        class DoubleSigner:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def place_order(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                return {"orderID": "poly-1", "status": "matched", "filled_size": kwargs["size"]}

        double = DoubleSigner()
        adapter = PolymarketExecutionAdapter(signing_client=double)
        result = await adapter.place_buy(
            market_id="0xtoken", side=Side.YES, size=7, limit_price=Decimal("0.40")
        )
        assert double.calls[0] == {
            "token_id": "0xtoken",
            "side": "BUY",
            "size": 7,
            "price": "0.40",
        }
        assert result.venue is Venue.POLYMARKET
        assert result.order_id == "poly-1"
        assert result.status is OrderStatus.FILLED
        assert result.filled_size == 7
