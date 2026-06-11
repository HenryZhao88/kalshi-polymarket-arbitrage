"""Versioned per-series fee overrides for Kalshi.

Loaded at runtime from GET /trade-api/v2/series/fee_changes (unauthenticated,
live-verified 2026-06-11; doc:
https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes).
Each change is {series_ticker, fee_type, fee_multiplier, scheduled_ts}; the change
with the latest scheduled_ts ≤ now is in force. `flat` fee semantics are
undocumented (docs/VERIFICATION.md §1.3) so such series are never priced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class FeeType(StrEnum):
    QUADRATIC = "quadratic"
    QUADRATIC_WITH_MAKER_FEES = "quadratic_with_maker_fees"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class FeeChange:
    change_id: str
    series_ticker: str
    fee_type: FeeType
    fee_multiplier: Decimal
    scheduled_ts: datetime

    @property
    def is_priceable(self) -> bool:
        """Only quadratic schedules have verified semantics; `flat` goes to review."""
        return self.fee_type is not FeeType.FLAT


@dataclass(frozen=True)
class OverrideTable:
    _by_series: dict[str, list[FeeChange]]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OverrideTable:
        by_series: dict[str, list[FeeChange]] = defaultdict(list)
        for entry in payload["series_fee_change_arr"]:
            raw_type = entry["fee_type"]
            try:
                fee_type = FeeType(raw_type)
            except ValueError as exc:
                raise ValueError(f"unknown fee_type {raw_type!r}") from exc
            change = FeeChange(
                change_id=entry["id"],
                series_ticker=entry["series_ticker"],
                fee_type=fee_type,
                fee_multiplier=Decimal(str(entry["fee_multiplier"])),
                scheduled_ts=datetime.fromisoformat(entry["scheduled_ts"].replace("Z", "+00:00")),
            )
            by_series[change.series_ticker].append(change)
        for changes in by_series.values():
            changes.sort(key=lambda c: c.scheduled_ts)
        return cls(_by_series=dict(by_series))

    def effective(self, series_ticker: str, at: datetime) -> FeeChange | None:
        """Latest change scheduled at or before `at`, or None (= general schedule)."""
        candidates = [c for c in self._by_series.get(series_ticker, []) if c.scheduled_ts <= at]
        return candidates[-1] if candidates else None
