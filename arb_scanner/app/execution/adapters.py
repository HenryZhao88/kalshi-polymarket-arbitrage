"""Venue execution adapters — DISABLED BY DEFAULT, reached only via the gated
two-leg executor (execution/executor.py).

The executor speaks one normalized ``ExecutionClient`` protocol; each venue gets
an adapter that translates a normalized buy/sell into the venue's order API:

- Kalshi: fully implemented over the authenticated ``KalshiRestClient`` order
  endpoints (prices are integer cents, side is yes/no).
- Polymarket: order *signing* (EIP-712) is delegated to the official
  ``py-clob-client`` dependency, which we do not vendor. The adapter imports it
  lazily and FAILS CLOSED with a clear error if it is absent, rather than
  hand-rolling signing constants (project rule: no guessed venue constants).

Adapters perform plain authenticated I/O; they do NOT enforce the execution
gates. Gating (mode, live_order_placement, geoblock, kill switch, size caps,
balance preflight) lives in the executor so there is one choke point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Protocol

from arb_scanner.app.clients.kalshi_rest import KalshiRestClient
from arb_scanner.app.types import Money, Side, Venue


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    RESTING = "resting"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Normalized outcome of a single venue order."""

    venue: Venue
    order_id: str | None
    status: OrderStatus
    requested_size: int
    filled_size: int
    avg_price: Decimal | None
    raw: dict[str, object]

    @property
    def fully_filled(self) -> bool:
        return self.status is OrderStatus.FILLED and self.filled_size >= self.requested_size

    @property
    def any_fill(self) -> bool:
        return self.filled_size > 0


class ExecutionClient(Protocol):
    """One normalized order surface the executor drives, per venue.

    ``market_id`` is the venue's order identifier for the leg (Kalshi ticker /
    Polymarket token id). Prices are probabilities in dollars (0–1).
    """

    venue: Venue

    async def available_balance(self) -> Money: ...

    async def place_buy(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult: ...

    async def place_sell(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult: ...

    async def cancel(self, order_id: str) -> None: ...


def _price_to_cents(price: Decimal) -> int:
    """Probability dollars (0–1) → integer cents (1–99), clamped."""
    cents = int((price * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return max(1, min(99, cents))


def _kalshi_status(raw_status: str, filled: int, requested: int) -> OrderStatus:
    mapping = {
        "executed": OrderStatus.FILLED,
        "filled": OrderStatus.FILLED,
        "resting": OrderStatus.RESTING,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
    }
    status = mapping.get(raw_status.lower(), OrderStatus.UNKNOWN)
    if status is OrderStatus.RESTING and filled > 0:
        return OrderStatus.PARTIAL if filled < requested else OrderStatus.FILLED
    if filled >= requested and requested > 0 and status is OrderStatus.UNKNOWN:
        return OrderStatus.FILLED
    return status


class KalshiExecutionAdapter:
    """Normalized order surface over the authenticated Kalshi REST client."""

    venue = Venue.KALSHI

    def __init__(self, client: KalshiRestClient) -> None:
        self._client = client

    async def available_balance(self) -> Money:
        payload = await self._client.get_balance()
        # Kalshi balance is integer cents.
        return Money.from_cents(int(payload.get("balance", 0)))

    def _side_value(self, side: Side) -> str:
        return "yes" if side is Side.YES else "no"

    async def _order(
        self,
        *,
        action: str,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None,
    ) -> OrderResult:
        cents = _price_to_cents(limit_price)
        side_value = self._side_value(side)
        kwargs: dict[str, object] = {
            "ticker": market_id,
            "side": side_value,
            "action": action,
            "count": size,
            "client_order_id": client_order_id,
        }
        if side is Side.YES:
            kwargs["yes_price_cents"] = cents
        else:
            kwargs["no_price_cents"] = cents
        payload = await self._client.create_order(**kwargs)  # type: ignore[arg-type]
        order = payload.get("order", payload)
        filled = int(order.get("filled_count", order.get("count_filled", 0)) or 0)
        avg_cents = order.get("average_fill_price") or order.get("yes_price") or cents
        avg_price = Decimal(str(avg_cents)) / 100 if avg_cents is not None else None
        return OrderResult(
            venue=self.venue,
            order_id=str(order.get("order_id")) if order.get("order_id") else None,
            status=_kalshi_status(str(order.get("status", "")), filled, size),
            requested_size=size,
            filled_size=filled,
            avg_price=avg_price,
            raw=dict(payload),
        )

    async def place_buy(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult:
        return await self._order(
            action="buy",
            market_id=market_id,
            side=side,
            size=size,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )

    async def place_sell(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult:
        return await self._order(
            action="sell",
            market_id=market_id,
            side=side,
            size=size,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )

    async def cancel(self, order_id: str) -> None:
        await self._client.cancel_order(order_id)


class PolymarketExecutionUnavailable(RuntimeError):
    """Raised when the Polymarket execution dependency is not installed."""


class PolymarketExecutionAdapter:
    """Normalized order surface for Polymarket.

    Order signing (EIP-712 / L1) is delegated to the official ``py-clob-client``
    package, imported lazily. We deliberately do not reimplement the signing
    domain constants here; if the dependency is missing the adapter fails closed.
    """

    venue = Venue.POLYMARKET

    def __init__(self, signing_client: object | None = None) -> None:
        # ``signing_client`` is an instantiated py_clob_client.ClobClient (or a
        # test double exposing the same create/post/cancel surface). When None,
        # construction is allowed but every order call fails closed so the rest
        # of the wiring can be imported and unit-tested without the dependency.
        self._client = signing_client

    @staticmethod
    def is_available() -> bool:
        try:
            import py_clob_client  # type: ignore  # noqa: F401
        except ImportError:
            return False
        return True

    def _require_client(self) -> object:
        if self._client is None:
            raise PolymarketExecutionUnavailable(
                "Polymarket live execution requires an instantiated py-clob-client "
                "signing client (install the 'execution' extra and provide "
                "ARB_POLYMARKET_PRIVATE_KEY); none was supplied"
            )
        return self._client

    async def available_balance(self) -> Money:
        client = self._require_client()
        # py-clob-client exposes collateral balance via get_balance_allowance.
        getter = getattr(client, "get_available_balance", None)
        if getter is None:
            raise PolymarketExecutionUnavailable("signing client does not expose a balance method")
        raw = await _maybe_await(getter())
        return Money.from_dollars(Decimal(str(raw)))

    async def place_buy(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult:
        return await self._place(market_id, "BUY", size, limit_price)

    async def place_sell(
        self,
        *,
        market_id: str,
        side: Side,
        size: int,
        limit_price: Decimal,
        client_order_id: str | None = None,
    ) -> OrderResult:
        return await self._place(market_id, "SELL", size, limit_price)

    async def _place(
        self, token_id: str, action: str, size: int, limit_price: Decimal
    ) -> OrderResult:
        client = self._require_client()
        placer = getattr(client, "place_order", None)
        if placer is None:
            raise PolymarketExecutionUnavailable("signing client does not expose place_order")
        payload = await _maybe_await(
            placer(token_id=token_id, side=action, size=size, price=str(limit_price))
        )
        data = payload if isinstance(payload, dict) else {}
        filled = int(data.get("filled_size", data.get("size_matched", 0)) or 0)
        order_id = data.get("orderID") or data.get("order_id")
        status_raw = str(data.get("status", "")).lower()
        status = {
            "matched": OrderStatus.FILLED,
            "filled": OrderStatus.FILLED,
            "live": OrderStatus.RESTING,
            "delayed": OrderStatus.RESTING,
            "canceled": OrderStatus.CANCELED,
            "cancelled": OrderStatus.CANCELED,
        }.get(status_raw, OrderStatus.UNKNOWN)
        if status is OrderStatus.UNKNOWN and filled >= size and size > 0:
            status = OrderStatus.FILLED
        return OrderResult(
            venue=self.venue,
            order_id=str(order_id) if order_id else None,
            status=status,
            requested_size=size,
            filled_size=filled,
            avg_price=limit_price,
            raw=data,
        )

    async def cancel(self, order_id: str) -> None:
        client = self._require_client()
        canceler = getattr(client, "cancel", None)
        if canceler is None:
            raise PolymarketExecutionUnavailable("signing client does not expose cancel")
        await _maybe_await(canceler(order_id))


async def _maybe_await(value: object) -> object:
    """Await ``value`` if it is awaitable; otherwise return it as-is.

    py-clob-client's surface is synchronous; a test double may be async. This
    lets one adapter drive both without branching at every call site.
    """
    if hasattr(value, "__await__"):
        return await value  # value is Awaitable here
    return value
