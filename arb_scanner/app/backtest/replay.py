"""Replay stored snapshots through the fill simulator and render reports."""

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
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.models import BookSnapshotRow
from arb_scanner.app.types import Money


async def load_frames(database_url: str) -> dict[str, list[BookFrame]]:
    """Snapshot rows grouped by market, ordered by capture time."""
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


def replay_market(
    frames: list[BookFrame], *, size: Decimal = Decimal(100), simulator: FillSimulator | None = None
) -> list[TradeRecord]:
    """Simulate a buy at every frame; realized cost comes from the post-latency book."""
    simulator = simulator or FillSimulator()
    records: list[TradeRecord] = []
    for i, frame in enumerate(frames):
        best_ask = frame.book.best_ask
        if best_ask is None:
            continue
        fill = simulator.execute_buy(frames, i, size)
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
                alerted=True,
            )
        )
    return records


def render_html_report(metrics_by_market: dict[str, BacktestMetrics], out_path: Path) -> None:
    """Plotly sensitivity/summary report (HTML, works headless)."""
    import plotly.graph_objects as go

    out_path.parent.mkdir(parents=True, exist_ok=True)
    markets = list(metrics_by_market)
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="avg net (estimated $)",
            x=markets,
            y=[float(m.avg_net_estimated) for m in metrics_by_market.values()],
        )
    )
    fig.add_trace(
        go.Bar(
            name="capital utilization",
            x=markets,
            y=[float(m.capital_utilization) for m in metrics_by_market.values()],
        )
    )
    fig.update_layout(title="arb-scanner backtest summary", barmode="group")
    fig.write_html(str(out_path))


def run_replay_cli(args: argparse.Namespace, settings: Settings) -> int:
    database_url = getattr(args, "database_url", None) or settings.database_url
    by_market = asyncio.run(load_frames(database_url))
    if not by_market:
        print("no orderbook snapshots in storage yet — run `scan` first")
        return 1
    metrics_by_market: dict[str, Any] = {}
    for market, frames in by_market.items():
        metrics = compute_metrics(replay_market(frames))
        metrics_by_market[market] = metrics
        print(
            f"{market}: trades={metrics.trades} alerts={metrics.alerts} "
            f"hit_rate={metrics.hit_rate:.2f} "
            f"slippage_realization={metrics.slippage_realization} "
            f"rejections={metrics.rejection_histogram}"
        )
    if getattr(args, "out", None):
        render_html_report(metrics_by_market, Path(args.out))
        print(f"report written to {args.out}")
    return 0
