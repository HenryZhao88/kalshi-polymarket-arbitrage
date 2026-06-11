"""Polymarket market-channel WebSocket: subscription + typed event parsing.

wss://ws-subscriptions-clob.polymarket.com/ws/market, public, no auth.
Events: book, price_change, tick_size_change, last_trade_price, and (with
custom_feature_enabled) best_bid_ask, new_market, market_resolved
(https://docs.polymarket.com/market-data/websocket/market-channel, 2026-06-11).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def subscribe_command(asset_ids: list[str], custom_features: bool = True) -> str:
    return json.dumps(
        {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": custom_features,
        }
    )


@dataclass(frozen=True, slots=True)
class BookEvent:
    asset_id: str
    payload: dict[str, Any]  # full L2 ladders; parsed via books.polymarket_book


@dataclass(frozen=True, slots=True)
class PriceChangeEvent:
    asset_id: str
    price: Decimal
    size: Decimal
    side: str
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(frozen=True, slots=True)
class LastTradeEvent:
    asset_id: str
    price: Decimal
    size: Decimal
    side: str
    fee_rate_bps: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketResolvedEvent:
    condition_id: str
    winning_asset_id: str | None
    winning_outcome: str | None


@dataclass(frozen=True, slots=True)
class NewMarketEvent:
    condition_id: str
    fee_schedule: dict[str, Any] | None  # {exponent, rate, taker_only, rebate_rate}


@dataclass(frozen=True, slots=True)
class TickSizeChangeEvent:
    asset_id: str
    old_tick_size: Decimal
    new_tick_size: Decimal


Event = (
    BookEvent
    | PriceChangeEvent
    | LastTradeEvent
    | MarketResolvedEvent
    | NewMarketEvent
    | TickSizeChangeEvent
)


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, "") else None


def parse_event(message: dict[str, Any]) -> list[Event]:
    """One WS message may fan out to several typed events (price_changes array)."""
    event_type = message.get("event_type") or message.get("type")
    if event_type == "book":
        return [BookEvent(asset_id=message["asset_id"], payload=message)]
    if event_type == "price_change":
        events: list[Event] = []
        for change in message.get("price_changes", []):
            events.append(
                PriceChangeEvent(
                    asset_id=change["asset_id"],
                    price=Decimal(change["price"]),
                    size=Decimal(change["size"]),
                    side=change["side"],
                    best_bid=_dec(change.get("best_bid")),
                    best_ask=_dec(change.get("best_ask")),
                )
            )
        return events
    if event_type == "last_trade_price":
        return [
            LastTradeEvent(
                asset_id=message["asset_id"],
                price=Decimal(message["price"]),
                size=Decimal(message["size"]),
                side=message["side"],
                fee_rate_bps=_dec(message.get("fee_rate_bps")),
            )
        ]
    if event_type == "market_resolved":
        return [
            MarketResolvedEvent(
                condition_id=message.get("condition_id") or message.get("market", ""),
                winning_asset_id=message.get("winning_asset_id"),
                winning_outcome=message.get("winning_outcome"),
            )
        ]
    if event_type == "new_market":
        return [
            NewMarketEvent(
                condition_id=message.get("condition_id") or message.get("market", ""),
                fee_schedule=message.get("fee_schedule"),
            )
        ]
    if event_type == "tick_size_change":
        return [
            TickSizeChangeEvent(
                asset_id=message["asset_id"],
                old_tick_size=Decimal(str(message["old_tick_size"])),
                new_tick_size=Decimal(str(message["new_tick_size"])),
            )
        ]
    return []
