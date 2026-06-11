"""Scan orchestration: discovery → matching → economics → risk → alert/persist.

Venue clients and alert sinks are injected; this module never constructs them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from arb_scanner.app.alerts.base import AlertPayload, AlertSink
from arb_scanner.app.books.kalshi_book import KalshiBook
from arb_scanner.app.books.polymarket_book import from_clob_payload
from arb_scanner.app.economics import OpportunityEvaluation, evaluate_both_directions
from arb_scanner.app.fees.polymarket import resolve_fee_schedule
from arb_scanner.app.fees.slippage import FixedCentsSlippage, SlippageModel
from arb_scanner.app.markets.discovery import (
    MatchedPair,
    evaluate_pair,
    kalshi_is_scannable,
)
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.risk.controls import OpportunityRisk, RiskLimits, check
from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import Side

log = logging.getLogger("arb_scanner.scanner")


class KalshiClientProto(Protocol):
    async def get_markets(
        self,
        *,
        limit: int = ...,
        status: str | None = ...,
        series_ticker: str | None = ...,
        cursor: str | None = ...,
    ) -> dict[str, Any]: ...
    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]: ...


class GammaClientProto(Protocol):
    async def get_markets(
        self, *, limit: int = ..., offset: int = ..., closed: bool = ...
    ) -> list[dict[str, Any]]: ...


class ClobClientProto(Protocol):
    async def get_book(self, token_id: str) -> dict[str, Any]: ...


@dataclass
class ScanReport:
    pairs_considered: int = 0
    pairs_accepted: int = 0
    opportunities: list[tuple[MatchedPair, OpportunityEvaluation, list[str]]] = field(
        default_factory=list
    )

    def render_lines(self) -> list[str]:
        lines = [
            f"pairs considered: {self.pairs_considered}, accepted: {self.pairs_accepted}"
        ]
        for pair, evaluation, reasons in self.opportunities:
            decision = "ALERT" if not reasons else f"rejected: {'; '.join(reasons)}"
            lines.append(
                f"[{pair.kalshi_ticker} <> {pair.poly_condition_id[:14]}…] "
                f"{evaluation.direction} size={evaluation.size} "
                f"k_vwap={evaluation.kalshi_leg.vwap} p_vwap={evaluation.poly_leg.vwap} | "
                f"fees: K ${evaluation.fees.kalshi_fee.to_dollars()} "
                f"P ${evaluation.fees.polymarket_fee.to_dollars()} "
                f"slip ${evaluation.fees.expected_slippage.to_dollars()} | "
                f"net ${evaluation.net.to_dollars()} "
                f"(roi {evaluation.simple_return:.2%}, "
                f"annual {evaluation.annualized_return:.1%}) → {decision}"
            )
        return lines


async def scan_once(
    *,
    kalshi: KalshiClientProto,
    gamma: GammaClientProto,
    clob: ClobClientProto,
    sinks: list[AlertSink],
    limits: RiskLimits | None = None,
    exposure: ExposureTracker | None = None,
    kill_switch: KillSwitch | None = None,
    slippage_model: SlippageModel | None = None,
    size: int = 100,
    market_limit: int = 100,
    min_similarity_prefilter: float = 0.35,
) -> ScanReport:
    limits = limits or RiskLimits()
    exposure = exposure or ExposureTracker()
    kill_switch = kill_switch or KillSwitch()
    slippage_model = slippage_model or FixedCentsSlippage(cents_per_share=Decimal("0.5"))
    report = ScanReport()

    kalshi_markets = [
        m
        for m in (await kalshi.get_markets(limit=market_limit)).get("markets", [])
        if kalshi_is_scannable(m)
    ]
    poly_markets = await gamma.get_markets(limit=market_limit)

    for k_market in kalshi_markets:
        for p_market in poly_markets:
            pair = evaluate_pair(k_market, p_market)
            if pair is None:
                continue
            if pair.confidence < min_similarity_prefilter:
                continue
            report.pairs_considered += 1
            if pair.status is not MatchStatus.ACCEPTED:
                continue
            report.pairs_accepted += 1

            kalshi_book = KalshiBook.from_rest_payload(
                pair.kalshi_ticker, await kalshi.get_orderbook(pair.kalshi_ticker)
            )
            poly_yes = from_clob_payload(await clob.get_book(pair.poly_yes_token_id), Side.YES)
            poly_no = from_clob_payload(await clob.get_book(pair.poly_no_token_id), Side.NO)

            category = (p_market.get("tags") or [None])[0]
            fee_schedule = resolve_fee_schedule(
                market_schedule=None, category=category.lower() if category else None
            )

            evaluations = evaluate_both_directions(
                kalshi_yes_view=kalshi_book.view(Side.YES),
                kalshi_no_view=kalshi_book.view(Side.NO),
                poly_yes_book=poly_yes,
                poly_no_book=poly_no,
                size=size,
                poly_fee_schedule=fee_schedule,
                slippage_model=slippage_model,
            )
            for evaluation in evaluations:
                reasons = check(
                    OpportunityRisk(
                        locked_capital=evaluation.locked,
                        net_profit=evaluation.net,
                        simple_return=evaluation.simple_return,
                        annualized_return=evaluation.annualized_return,
                        match_confidence=pair.confidence,
                        fill_fraction=Decimal(1)
                        if not evaluation.partial_fill_risk
                        else evaluation.kalshi_leg.fill_fraction,
                        hold_days=Decimal(30),
                        quote_age_seconds=0.0,
                        category=category.lower() if category else None,
                    ),
                    limits,
                    exposure,
                    kill_switch,
                )
                report.opportunities.append((pair, evaluation, reasons))
                if not reasons:
                    payload = AlertPayload(
                        kalshi_ticker=pair.kalshi_ticker,
                        poly_condition_id=pair.poly_condition_id,
                        direction=evaluation.direction.value,
                        confidence=pair.confidence,
                        size=evaluation.size,
                        depth_summary=(
                            f"k_levels={evaluation.kalshi_leg.levels_consumed} "
                            f"p_levels={evaluation.poly_leg.levels_consumed}"
                            + (" PARTIAL" if evaluation.partial_fill_risk else "")
                        ),
                        fees=evaluation.fees,
                        net_edge=evaluation.net,
                        simple_return=evaluation.simple_return,
                        annualized_return=evaluation.annualized_return,
                        break_even_slippage_per_share=evaluation.break_even_slippage_per_share,
                        break_even_extra_fees=evaluation.break_even_extra_fees,
                        snapshot_id=None,
                    )
                    for sink in sinks:
                        try:
                            await sink.send(payload)
                        except Exception:
                            log.exception("alert sink %r failed", type(sink).__name__)

    return report
