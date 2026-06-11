"""Kalshi per-series fee override table tests.

Payload shape mirrors GET /trade-api/v2/series/fee_changes (live-verified 2026-06-11,
fixture tests/fixtures/live_2026-06-11/kalshi_fee_changes.json).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from arb_scanner.app.fees.overrides import FeeType, OverrideTable

PAYLOAD = {
    "series_fee_change_arr": [
        {
            "fee_multiplier": 0,
            "fee_type": "quadratic",
            "id": "a",
            "scheduled_ts": "2026-06-08T04:30:00Z",
            "series_ticker": "KXHYPEPERP",
        },
        {
            "fee_multiplier": 1,
            "fee_type": "quadratic_with_maker_fees",
            "id": "b",
            "scheduled_ts": "2026-06-05T22:00:00Z",
            "series_ticker": "KXWCGAME",
        },
        {
            "fee_multiplier": 0.5,
            "fee_type": "quadratic",
            "id": "c",
            "scheduled_ts": "2026-01-01T00:00:00Z",
            "series_ticker": "KXHYPEPERP",
        },
        {
            "fee_multiplier": 2,
            "fee_type": "flat",
            "id": "d",
            "scheduled_ts": "2026-01-01T00:00:00Z",
            "series_ticker": "KXFLATTEST",
        },
    ]
}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestOverrideTable:
    def test_no_override_returns_none(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        assert table.effective("KXUNKNOWN", at("2026-06-10T00:00:00")) is None

    def test_latest_effective_change_wins(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        change = table.effective("KXHYPEPERP", at("2026-06-10T00:00:00"))
        assert change is not None
        assert change.fee_multiplier == Decimal(0)

    def test_earlier_version_selected_before_cutover(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        change = table.effective("KXHYPEPERP", at("2026-03-01T00:00:00"))
        assert change is not None
        assert change.fee_multiplier == Decimal("0.5")

    def test_future_changes_not_applied(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        assert table.effective("KXHYPEPERP", at("2025-12-31T00:00:00")) is None

    def test_maker_fee_flag(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        change = table.effective("KXWCGAME", at("2026-06-10T00:00:00"))
        assert change is not None
        assert change.fee_type is FeeType.QUADRATIC_WITH_MAKER_FEES

    def test_flat_fee_type_is_not_priceable(self) -> None:
        # `flat` semantics are undocumented (VERIFICATION.md §1.3): never price it.
        table = OverrideTable.from_payload(PAYLOAD)
        change = table.effective("KXFLATTEST", at("2026-06-10T00:00:00"))
        assert change is not None
        assert change.fee_type is FeeType.FLAT
        assert not change.is_priceable

    def test_quadratic_is_priceable(self) -> None:
        table = OverrideTable.from_payload(PAYLOAD)
        change = table.effective("KXHYPEPERP", at("2026-06-10T00:00:00"))
        assert change is not None
        assert change.is_priceable

    def test_unknown_fee_type_rejected(self) -> None:
        bad = {
            "series_fee_change_arr": [
                {
                    "fee_multiplier": 1,
                    "fee_type": "exotic_new_type",
                    "id": "x",
                    "scheduled_ts": "2026-01-01T00:00:00Z",
                    "series_ticker": "KXNEW",
                }
            ]
        }
        with pytest.raises(ValueError, match="exotic_new_type"):
            OverrideTable.from_payload(bad)
