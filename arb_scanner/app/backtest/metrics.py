"""Backtest metrics: hit rate, gross vs net edge, slippage realization, capital
utilization, time-weighted returns, rejection histogram (SPEC Phase 6)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from arb_scanner.app.types import Money


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One simulated (or realized) two-leg trade for metric aggregation."""

    gross: Money
    net_estimated: Money
    net_realized: Money | None
    estimated_slippage: Money
    realized_slippage: Money | None
    locked: Money
    hold_days: Decimal
    alerted: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    trades: int
    alerts: int
    hit_rate: float  # share of alerted trades with positive realized net
    avg_gross: Decimal
    avg_net_estimated: Decimal
    avg_net_realized: Decimal | None
    slippage_realization: Decimal | None  # realized / estimated
    capital_utilization: Decimal  # net per locked dollar
    time_weighted_return: Decimal  # sum(net) / sum(locked × hold)e
    rejection_histogram: dict[str, int]


def compute_metrics(records: list[TradeRecord]) -> BacktestMetrics:
    if not records:
        return BacktestMetrics(
            trades=0,
            alerts=0,
            hit_rate=0.0,
            avg_gross=Decimal(0),
            avg_net_estimated=Decimal(0),
            avg_net_realized=None,
            slippage_realization=None,
            capital_utilization=Decimal(0),
            time_weighted_return=Decimal(0),
            rejection_histogram={},
        )
    n = len(records)
    alerted = [r for r in records if r.alerted]
    realized = [r for r in alerted if r.net_realized is not None]
    wins = sum(1 for r in realized if r.net_realized is not None and r.net_realized > Money.zero())

    est_slip = sum((r.estimated_slippage.to_dollars() for r in realized), Decimal(0))
    real_slip = sum(
        (r.realized_slippage.to_dollars() for r in realized if r.realized_slippage is not None),
        Decimal(0),
    )
    total_locked = sum((r.locked.to_dollars() for r in records), Decimal(0))
    total_net = sum((r.net_estimated.to_dollars() for r in records), Decimal(0))
    locked_days = sum((r.locked.to_dollars() * r.hold_days for r in records), Decimal(0))

    histogram: Counter[str] = Counter()
    for record in records:
        for reason in record.rejection_reasons:
            histogram[" ".join(reason.split(" ")[:2])] += 1  # bucket by reason prefix

    return BacktestMetrics(
        trades=n,
        alerts=len(alerted),
        hit_rate=(wins / len(realized)) if realized else 0.0,
        avg_gross=sum((r.gross.to_dollars() for r in records), Decimal(0)) / n,
        avg_net_estimated=total_net / n,
        avg_net_realized=(
            sum(
                (r.net_realized.to_dollars() for r in realized if r.net_realized is not None),
                Decimal(0),
            )
            / len(realized)
            if realized
            else None
        ),
        slippage_realization=(real_slip / est_slip) if est_slip else None,
        capital_utilization=(total_net / total_locked) if total_locked else Decimal(0),
        time_weighted_return=(total_net / locked_days * 365 if locked_days else Decimal(0)),
        rejection_histogram=dict(histogram),
    )
