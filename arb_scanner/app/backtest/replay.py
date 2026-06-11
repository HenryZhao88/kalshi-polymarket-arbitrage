"""Replay stored paired opportunities; isolated-book replay is experimental only."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from arb_scanner.app.backtest.datasets import BookFrame, from_snapshot_row
from arb_scanner.app.backtest.fills import FillOutcome, FillSimulator
from arb_scanner.app.backtest.metrics import BacktestMetrics, TradeRecord, compute_metrics
from arb_scanner.app.config import Settings
from arb_scanner.app.economics import Direction, evaluate_direction
from arb_scanner.app.fees.polymarket import FeeRateSource, FeeSchedule
from arb_scanner.app.fees.profit import FeeBreakdown
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.models import BookSnapshotRow, OpportunityRow
from arb_scanner.app.types import Money


class PairedSnapshotRequiredError(ValueError):
    """Raised when storage lacks scan-produced two-leg opportunity evidence."""


class StoredSlippage:
    def __init__(self, cost: Money) -> None:
        self._cost = cost

    def estimate(self, size: int, quoted_edge: Money) -> Money:
        return self._cost


async def load_frames(database_url: str) -> dict[str, list[BookFrame]]:
    """Load legacy isolated books for the experimental single-book utility."""
    engine = make_engine(database_url)
    await init_models(engine)
    factory = make_session_factory(engine)
    by_market: dict[str, list[BookFrame]] = {}
    async with factory() as session:
        result = await session.execute(
            select(BookSnapshotRow).order_by(BookSnapshotRow.captured_at)
        )
        for row in result.scalars():
            if row.payload.get("format") != "orderbook":
                continue
            frame = from_snapshot_row(row.payload, row.captured_at, row.venue, row.market_id)
            by_market.setdefault(f"{row.venue}:{row.market_id}", []).append(frame)
    await engine.dispose()
    return by_market


async def load_paired_opportunities(database_url: str) -> list[OpportunityRow]:
    """Load complete paired opportunity snapshots produced by scanning."""
    engine = make_engine(database_url)
    await init_models(engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            result = await session.execute(
                select(OpportunityRow).order_by(OpportunityRow.created_at)
            )
            return [
                row for row in result.scalars() if paired_snapshot_is_complete(row.book_snapshot)
            ]
    finally:
        await engine.dispose()


def paired_snapshot_is_complete(payload: dict[str, Any]) -> bool:
    required = ("kalshi_yes", "kalshi_no", "poly_yes", "poly_no")
    return payload.get("format") == "paired_opportunity" and all(
        isinstance(payload.get(name), dict) for name in required
    )


def require_paired_snapshots(rows: list[OpportunityRow]) -> None:
    if rows:
        return
    raise PairedSnapshotRequiredError(
        "no complete paired opportunity snapshots in storage; run `arb-scanner dry-run` "
        "with ARB_PERSIST_SCANS=true. Legacy isolated order-book rows are insufficient."
    )


def single_book_replay(
    frames: list[BookFrame],
    *,
    size: Decimal = Decimal(100),
    simulator: FillSimulator | None = None,
) -> list[TradeRecord]:
    """Experimental isolated-book fill simulation, not an arbitrage backtest."""
    simulator = simulator or FillSimulator()
    records: list[TradeRecord] = []
    for index, frame in enumerate(frames):
        best_ask = frame.book.best_ask
        if best_ask is None:
            continue
        fill = simulator.execute_buy(frames, index, size)
        quoted_cost = size * best_ask.price
        if fill.vwap is None or fill.outcome in (FillOutcome.STALE_QUOTE, FillOutcome.NO_DEPTH):
            records.append(
                TradeRecord(
                    gross=Money.zero(),
                    net_estimated=Money.zero(),
                    net_realized=None,
                    estimated_slippage=Money.zero(),
                    realized_slippage=None,
                    locked=Money.from_dollars(quoted_cost.quantize(Decimal("0.000001"))),
                    hold_days=Decimal(1),
                    alerted=False,
                    rejection_reasons=(fill.outcome.value,),
                )
            )
            continue
        realized_cost = fill.filled * fill.vwap
        slip = realized_cost - (fill.filled * best_ask.price)
        records.append(
            TradeRecord(
                gross=Money.from_dollars(quoted_cost.quantize(Decimal("0.000001"))),
                net_estimated=Money.zero(),
                net_realized=Money.from_dollars((-slip).quantize(Decimal("0.000001"))),
                estimated_slippage=Money.zero(),
                realized_slippage=Money.from_dollars(slip.quantize(Decimal("0.000001"))),
                locked=Money.from_dollars(realized_cost.quantize(Decimal("0.000001"))),
                hold_days=Decimal(1),
                alerted=False,
                rejection_reasons=("experimental single-book simulation",),
            )
        )
    return records


def _money_field(payload: dict[str, Any], name: str) -> Money:
    return Money.from_dollars(str(payload.get(name, "0")))


def replay_paired_opportunity(row: OpportunityRow) -> TradeRecord:
    """Re-evaluate both-leg economics from one scan-produced evidence bundle."""
    payload = row.book_snapshot
    if not paired_snapshot_is_complete(payload):
        raise PairedSnapshotRequiredError(f"opportunity {row.id} lacks a complete paired snapshot")

    def book(name: str) -> BookFrame:
        raw = payload[name]
        assert isinstance(raw, dict)
        return from_snapshot_row(raw, row.created_at, str(raw["venue"]), str(raw["market_id"]))

    direction = Direction(row.direction)
    kalshi = book("kalshi_yes" if direction is Direction.KALSHI_YES_POLY_NO else "kalshi_no")
    poly = book("poly_no" if direction is Direction.KALSHI_YES_POLY_NO else "poly_yes")
    assumptions = row.assumptions
    fees = row.fee_breakdown
    schedule = FeeSchedule(
        rate=Decimal(str(assumptions["polymarket_fee_rate"])),
        exponent=Decimal(str(assumptions["polymarket_fee_exponent"])),
        source=FeeRateSource(str(assumptions["polymarket_fee_source"])),
    )
    fixed_costs = FeeBreakdown(
        bridge_cost=_money_field(fees, "bridge_cost"),
        withdrawal_cost=_money_field(fees, "withdrawal_cost"),
        gas_cost=_money_field(fees, "gas_cost"),
        processor_cost=_money_field(fees, "processor_cost"),
        conversion_cost=_money_field(fees, "conversion_cost"),
        unknown_cost_buffer=_money_field(fees, "unknown_cost_buffer"),
    )
    requested_size = int(assumptions["requested_size"])
    raw_hold_days = assumptions.get("hold_days")
    hold_days = Decimal(str(raw_hold_days)) if raw_hold_days is not None else Decimal(365)
    evaluation = evaluate_direction(
        direction=direction,
        kalshi_view=kalshi.book,
        poly_book=poly.book,
        size=requested_size,
        poly_fee_schedule=schedule,
        slippage_model=StoredSlippage(_money_field(fees, "slippage_cost")),
        fixed_costs=fixed_costs,
        hold_days=hold_days,
    )
    rejection_reasons = tuple(filter(None, (row.rejection_reason or "").split("; ")))
    if evaluation is None:
        return TradeRecord(
            gross=Money.zero(),
            net_estimated=Money.zero(),
            net_realized=None,
            estimated_slippage=Money.zero(),
            realized_slippage=None,
            locked=Money.zero(),
            hold_days=hold_days,
            alerted=False,
            rejection_reasons=(*rejection_reasons, "paired snapshot has no common depth"),
        )
    return TradeRecord(
        gross=evaluation.gross,
        net_estimated=evaluation.net,
        net_realized=None,
        estimated_slippage=evaluation.fees.slippage_cost,
        realized_slippage=None,
        locked=evaluation.locked,
        hold_days=hold_days,
        alerted=row.decision == "alerted",
        rejection_reasons=rejection_reasons,
    )


def render_html_report(metrics_by_pair: dict[str, BacktestMetrics], out_path: Path) -> None:
    """Render paired-snapshot evaluation metrics; no realized P&L is implied."""
    import plotly.graph_objects as go

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pairs = list(metrics_by_pair)
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            name="avg net (estimated $)",
            x=pairs,
            y=[float(metric.avg_net_estimated) for metric in metrics_by_pair.values()],
        )
    )
    figure.add_trace(
        go.Bar(
            name="capital utilization",
            x=pairs,
            y=[float(metric.capital_utilization) for metric in metrics_by_pair.values()],
        )
    )
    figure.update_layout(title="arb-scanner paired-snapshot evaluation", barmode="group")
    figure.write_html(str(out_path))


def run_replay_cli(args: argparse.Namespace, settings: Settings) -> int:
    database_url = getattr(args, "database_url", None) or settings.database_url
    rows = asyncio.run(load_paired_opportunities(database_url))
    try:
        require_paired_snapshots(rows)
    except PairedSnapshotRequiredError as exc:
        print(str(exc))
        return 1

    metrics_by_pair: dict[str, BacktestMetrics] = {}
    for row in rows:
        key = f"pair:{row.pair_id}:opportunity:{row.id}"
        metrics = compute_metrics([replay_paired_opportunity(row)])
        metrics_by_pair[key] = metrics
        print(
            f"{key}: evaluations={metrics.trades} alerts={metrics.alerts} "
            f"avg_net_estimated={metrics.avg_net_estimated} "
            f"realized_net=unavailable rejections={metrics.rejection_histogram}"
        )
    if getattr(args, "out", None):
        render_html_report(metrics_by_pair, Path(args.out))
        print(f"report written to {args.out}")
    return 0
