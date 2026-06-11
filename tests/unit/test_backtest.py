"""Backtest dataset, fill simulator, metrics, and two-leg simulation tests."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from arb_scanner.app.backtest.datasets import (
    BookFrame,
    from_orderbook_history_payload,
    from_snapshot_row,
)
from arb_scanner.app.backtest.fills import FillOutcome, FillSimulator
from arb_scanner.app.backtest.metrics import TradeRecord, compute_metrics
from arb_scanner.app.backtest.replay import (
    PairedSnapshotRequiredError,
    paired_snapshot_is_complete,
    replay_paired_opportunity,
    require_paired_snapshots,
)
from arb_scanner.app.execution.simulator import simulate_two_leg
from arb_scanner.app.storage.models import OpportunityRow
from arb_scanner.app.types import BookLevel, Money, OrderBook, Side, Venue

D = Decimal
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def frame(at: datetime, asks: list[tuple[str, int]]) -> BookFrame:
    return BookFrame(
        captured_at=at,
        book=OrderBook(
            venue=Venue.POLYMARKET,
            market_id="tok",
            side=Side.YES,
            bids=(),
            asks=tuple(BookLevel(price=D(p), size=D(s)) for p, s in asks),
        ),
    )


class TestDatasets:
    def test_orderbook_history_fixture_parses(self) -> None:
        payload = json.loads(
            Path("tests/fixtures/live_2026-06-11/poly_orderbook_history.json").read_text()
        )
        frames = from_orderbook_history_payload(payload, Side.YES)
        assert frames
        assert frames == sorted(frames, key=lambda f: f.captured_at)
        assert frames[0].book.venue is Venue.POLYMARKET

    def test_snapshot_row_round_trip(self) -> None:
        rebuilt = from_snapshot_row(
            {
                "format": "orderbook",
                "side": "no",
                "bids": [["0.40", "10"]],
                "asks": [["0.45", "5"]],
                "timestamp_ms": 1700000000000,
            },
            T0,
            "polymarket",
            "tok",
        )
        assert rebuilt.book.side is Side.NO
        assert rebuilt.book.best_ask is not None
        assert rebuilt.book.best_ask.price == D("0.45")


class TestFillSimulator:
    def test_fills_against_post_latency_book(self) -> None:
        frames = [
            frame(T0, [("0.40", 100)]),
            frame(T0 + timedelta(milliseconds=200), [("0.43", 100)]),  # price moved
        ]
        sim = FillSimulator(latency_ms=250)
        fill = sim.execute_buy(frames, 0, D(50))
        # decision at T0, executes at T0+250ms → hits the 0.43 book, not 0.40
        assert fill.outcome is FillOutcome.FILLED
        assert fill.vwap == D("0.43")

    def test_partial_fill(self) -> None:
        fill = FillSimulator(latency_ms=0).execute_buy([frame(T0, [("0.40", 30)])], 0, D(100))
        assert fill.outcome is FillOutcome.PARTIAL
        assert fill.filled == D(30)

    def test_stale_quote_rejected(self) -> None:
        frames = [frame(T0, [("0.40", 100)])]
        sim = FillSimulator(latency_ms=60_000, max_quote_age=timedelta(seconds=30))
        fill = sim.execute_buy(frames, 0, D(10))
        assert fill.outcome is FillOutcome.STALE_QUOTE
        assert fill.filled == D(0)

    def test_no_depth(self) -> None:
        fill = FillSimulator(latency_ms=0).execute_buy([frame(T0, [])], 0, D(10))
        assert fill.outcome is FillOutcome.NO_DEPTH

    def test_extra_slippage_applied(self) -> None:
        fill = FillSimulator(latency_ms=0, extra_slippage_per_share=D("0.01")).execute_buy(
            [frame(T0, [("0.40", 100)])], 0, D(10)
        )
        assert fill.vwap == D("0.41")


class TestTwoLegSimulation:
    def test_matched_size_is_min_of_legs(self) -> None:
        result = simulate_two_leg(
            [frame(T0, [("0.90", 100)])],
            [frame(T0, [("0.03", 60)])],
            decision_index=0,
            size=D(100),
            simulator=FillSimulator(latency_ms=0),
        )
        assert result.both_filled
        assert result.matched_size == D(60)  # leg-size mismatch → partial-fill exposure


class TestPairedReplay:
    def test_replay_requires_complete_paired_snapshots(self) -> None:
        assert not paired_snapshot_is_complete({"format": "orderbook"})
        with pytest.raises(PairedSnapshotRequiredError, match="dry-run"):
            require_paired_snapshots([])

    def test_replays_two_leg_economics(self) -> None:
        def snapshot(venue: str, market_id: str, side: str, ask: str) -> dict[str, object]:
            return {
                "format": "orderbook",
                "venue": venue,
                "market_id": market_id,
                "side": side,
                "bids": [],
                "asks": [[ask, "100"]],
                "timestamp_ms": 1781178206000,
            }

        row = OpportunityRow(
            pair_id=None,
            direction="kalshi_yes_poly_no",
            size="100",
            gross_micros=7_000_000,
            net_micros=6_253_600,
            fee_breakdown={
                "bridge_cost": "0",
                "withdrawal_cost": "0",
                "gas_cost": "0",
                "processor_cost": "0",
                "conversion_cost": "0",
                "slippage_cost": "0",
                "unknown_cost_buffer": "0",
            },
            assumptions={
                "requested_size": 100,
                "hold_days": "30",
                "polymarket_fee_rate": "0.04",
                "polymarket_fee_exponent": "1",
                "polymarket_fee_source": "market_metadata",
            },
            book_snapshot={
                "format": "paired_opportunity",
                "kalshi_yes": snapshot("kalshi", "K", "yes", "0.90"),
                "kalshi_no": snapshot("kalshi", "K", "no", "0.95"),
                "poly_yes": snapshot("polymarket", "YES", "yes", "0.99"),
                "poly_no": snapshot("polymarket", "NO", "no", "0.03"),
            },
            decision="alerted",
            rejection_reason=None,
            created_at=T0,
        )
        record = replay_paired_opportunity(row)
        assert record.gross == Money.from_dollars("7")
        assert record.net_estimated == Money.from_dollars("6.2536")
        assert record.alerted
        assert record.net_realized is None


class TestMetrics:
    def test_empty(self) -> None:
        metrics = compute_metrics([])
        assert metrics.trades == 0 and metrics.hit_rate == 0.0

    def test_aggregation(self) -> None:
        records = [
            TradeRecord(
                gross=Money.from_dollars("7.00"),
                net_estimated=Money.from_dollars("6.25"),
                net_realized=Money.from_dollars("6.00"),
                estimated_slippage=Money.from_dollars("0.50"),
                realized_slippage=Money.from_dollars("0.75"),
                locked=Money.from_dollars("94"),
                hold_days=D(30),
                alerted=True,
            ),
            TradeRecord(
                gross=Money.from_dollars("3.00"),
                net_estimated=Money.from_dollars("-1.00"),
                net_realized=None,
                estimated_slippage=Money.from_dollars("0.50"),
                realized_slippage=None,
                locked=Money.from_dollars("50"),
                hold_days=D(10),
                alerted=False,
                rejection_reasons=("net $-1.00 below min", "ROI 0.001 < min"),
            ),
        ]
        metrics = compute_metrics(records)
        assert metrics.trades == 2 and metrics.alerts == 1
        assert metrics.hit_rate == 1.0
        assert metrics.avg_gross == D("5.00")
        assert metrics.slippage_realization == D("1.5")
        assert metrics.rejection_histogram == {"net $-1.00": 1, "ROI 0.001": 1}
