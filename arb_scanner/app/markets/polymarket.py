"""Normalized Polymarket Gamma market metadata used by discovery and diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from arb_scanner.app.fees.polymarket import FeeRateSource, FeeSchedule, fee_schedule_from_metadata


def _parse_json_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _category(payload: dict[str, Any]) -> str | None:
    if payload.get("category"):
        return str(payload["category"]).lower()
    for relation in (payload.get("categories") or [], payload.get("tags") or []):
        for item in relation:
            if isinstance(item, str):
                return item.lower()
            if isinstance(item, dict):
                value = item.get("label") or item.get("name") or item.get("slug")
                if value:
                    return str(value).lower()
    return None


def _event_value(payload: dict[str, Any], key: str) -> Any:
    for event in payload.get("events") or []:
        if isinstance(event, dict) and event.get(key):
            return event[key]
    return None


@dataclass(frozen=True, slots=True)
class PolymarketMarket:
    market_id: str
    question: str
    condition_id: str
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    end_time: datetime | None
    resolution_time: datetime | None
    category: str | None
    description: str
    resolution_source: str
    fee_schedule: FeeSchedule | None
    active: bool
    closed: bool
    resolved: bool
    archived: bool
    accepting_orders: bool
    liquidity: Decimal | None
    volume: Decimal | None
    raw: dict[str, Any]

    @classmethod
    def from_gamma(cls, payload: dict[str, Any]) -> PolymarketMarket:
        resolution_status = str(
            payload.get("umaResolutionStatus") or payload.get("umaResolutionStatuses") or ""
        ).lower()
        closed = bool(payload.get("closed"))
        return cls(
            market_id=str(payload.get("id") or ""),
            question=str(payload.get("question") or payload.get("title") or ""),
            condition_id=str(payload.get("conditionId") or ""),
            token_ids=_parse_json_strings(payload.get("clobTokenIds")),
            outcomes=_parse_json_strings(payload.get("outcomes")),
            end_time=_parse_time(payload.get("endDate") or payload.get("endDateIso")),
            resolution_time=_parse_time(payload.get("umaEndDate") or payload.get("umaEndDateIso")),
            category=_category(payload),
            description=str(payload.get("description") or ""),
            resolution_source=str(
                payload.get("resolutionSource") or _event_value(payload, "resolutionSource") or ""
            ),
            fee_schedule=fee_schedule_from_metadata(payload),
            active=bool(payload.get("active")),
            closed=closed,
            resolved=closed
            or bool(payload.get("automaticallyResolved"))
            or resolution_status in {"resolved", "finalized"},
            archived=bool(payload.get("archived")),
            accepting_orders=bool(payload.get("acceptingOrders", True)),
            liquidity=_decimal(payload.get("liquidityNum", payload.get("liquidity"))),
            volume=_decimal(payload.get("volumeNum", payload.get("volume"))),
            raw=dict(payload),
        )

    @property
    def scannable(self) -> bool:
        return (
            self.active
            and not self.closed
            and not self.resolved
            and not self.archived
            and self.accepting_orders
            and bool(self.question)
            and bool(self.condition_id)
            and len(self.token_ids) >= 2
            and self.raw.get("enableOrderBook", True) is not False
        )

    @property
    def fee_confidence(self) -> str:
        if self.fee_schedule is not None:
            return FeeRateSource.MARKET_METADATA.value
        if self.category is not None:
            return FeeRateSource.CATEGORY_DEFAULT.value
        return FeeRateSource.UNKNOWN.value

    def metadata_excerpt(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "condition_id": self.condition_id,
            "token_ids": list(self.token_ids),
            "outcomes": list(self.outcomes),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "resolution_time": (self.resolution_time.isoformat() if self.resolution_time else None),
            "category": self.category,
            "description": self.description[:1000],
            "resolution_source": self.resolution_source,
            "fee_rate": str(self.fee_schedule.rate) if self.fee_schedule else None,
            "fee_exponent": str(self.fee_schedule.exponent) if self.fee_schedule else None,
            "fee_confidence": self.fee_confidence,
            "active": self.active,
            "closed": self.closed,
            "resolved": self.resolved,
            "accepting_orders": self.accepting_orders,
            "liquidity": str(self.liquidity) if self.liquidity is not None else None,
            "volume": str(self.volume) if self.volume is not None else None,
        }
