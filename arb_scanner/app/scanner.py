"""Scan orchestration: discovery -> matching -> economics -> risk -> alert/persist."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from arb_scanner.app.alerts.base import AlertPayload, AlertSink
from arb_scanner.app.books.kalshi_book import KalshiBook
from arb_scanner.app.books.polymarket_book import from_clob_payload
from arb_scanner.app.books.snapshots import orderbook_snapshot_payload
from arb_scanner.app.economics import (
    CostAssumptions,
    OpportunityEvaluation,
    evaluate_both_directions,
)
from arb_scanner.app.fees.polymarket import (
    FeeRateSource,
    FeeSchedule,
    fee_schedule_from_metadata,
    resolve_fee_schedule,
)
from arb_scanner.app.fees.slippage import FixedCentsSlippage, SlippageModel
from arb_scanner.app.markets.discovery import (
    ManualReviewSort,
    MatchedPair,
    determination_time,
    evaluate_pair,
    kalshi_is_scannable,
    kalshi_outcome_entity,
    sort_manual_review_pairs,
)
from arb_scanner.app.markets.parsers import parse_features
from arb_scanner.app.markets.polymarket import PolymarketMarket
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.risk.controls import OpportunityRisk, RiskLimits, check
from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import OrderBook, Side, Venue
from arb_scanner.app.markets.verification import verify_pair, VerificationInputs

log = logging.getLogger("arb_scanner.scanner")


def _text_excerpt(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or "missing"


class KalshiClientProto(Protocol):
    async def get_all_markets(
        self,
        *,
        limit: int = ...,
        status: str | None = ...,
        series_ticker: str | None = ...,
        mve_filter: str | None = ...,
        max_pages: int = ...,
    ) -> list[dict[str, Any]]: ...

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]: ...


class GammaClientProto(Protocol):
    async def get_all_markets(
        self,
        *,
        page_size: int = ...,
        max_pages: int = ...,
        max_markets: int = ...,
        closed: bool = ...,
    ) -> Any: ...


class ClobClientProto(Protocol):
    async def get_book(self, token_id: str) -> dict[str, Any]: ...

    async def get_market_info(self, condition_id: str) -> dict[str, Any]: ...


class ScanStoreProto(Protocol):
    async def add_pair(self, pair: MatchedPair) -> int | None: ...

    async def add_snapshot(self, venue: str, market_id: str, payload: dict[str, Any]) -> int: ...

    async def add_opportunity(
        self,
        *,
        pair_id: int,
        evaluation: OpportunityEvaluation,
        decision: str,
        rejection_reason: str | None,
        assumptions: dict[str, Any],
        paired_snapshot: dict[str, Any],
    ) -> int: ...


@dataclass
class ScanReport:
    kalshi_markets_discovered: int = 0
    kalshi_markets_scannable: int = 0
    poly_markets_discovered: int = 0
    poly_markets_scannable: int = 0
    poly_pages_fetched: int = 0
    poly_rows_fetched: int = 0
    raw_title_candidates: int = 0
    structured_candidates: int = 0
    manual_review_candidates: int = 0
    rejected_candidates: int = 0
    pairs_considered: int = 0
    pairs_accepted: int = 0
    verification_verdicts: Counter[str] = field(default_factory=Counter)
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    manual_review_pairs: list[MatchedPair] = field(default_factory=list)
    rejected_pairs: list[MatchedPair] = field(default_factory=list)
    opportunities: list[tuple[MatchedPair, OpportunityEvaluation, list[str]]] = field(
        default_factory=list
    )

    def render_lines(self) -> list[str]:
        lines = [
            (
                f"markets: Kalshi discovered={self.kalshi_markets_discovered} "
                f"scannable={self.kalshi_markets_scannable}, "
                f"Polymarket discovered={self.poly_markets_discovered} "
                f"scannable={self.poly_markets_scannable} "
                f"pages={self.poly_pages_fetched} fetched={self.poly_rows_fetched}"
            ),
            (
                f"candidate funnel: raw_title={self.raw_title_candidates} "
                f"structured={self.structured_candidates} "
                f"manual_review={self.manual_review_candidates} "
                f"accepted={self.pairs_accepted} rejected={self.rejected_candidates}"
            ),
        ]
        if self.verification_verdicts:
            verdict_summary = ", ".join(
                f"{verdict}={count}" for verdict, count in self.verification_verdicts.most_common()
            )
            lines.append(f"verification: {verdict_summary}")
        if self.rejection_reasons:
            reason_summary = ", ".join(
                f"{reason}={count}" for reason, count in self.rejection_reasons.most_common()
            )
            lines.append(f"rejections by reason: {reason_summary}")
        else:
            lines.append("rejections by reason: none")
        for pair, evaluation, reasons in self.opportunities:
            decision = "ALERT" if not reasons else f"rejected: {'; '.join(reasons)}"
            size_text = (
                f"requested={evaluation.requested_size} executable={evaluation.executable_size}"
            )
            lines.append(
                f"[{pair.kalshi_ticker} <> {pair.poly_condition_id[:14]}...] "
                f"{evaluation.direction} {size_text} "
                f"k_vwap={evaluation.kalshi_leg.vwap} p_vwap={evaluation.poly_leg.vwap} | "
                f"fees: K ${evaluation.fees.kalshi_fee.to_dollars()} "
                f"P ${evaluation.fees.polymarket_fee.to_dollars()} "
                f"slip ${evaluation.fees.slippage_cost.to_dollars()} "
                f"unknown ${evaluation.fees.unknown_cost_buffer.to_dollars()} | "
                f"net ${evaluation.net.to_dollars()} "
                f"(roi {evaluation.simple_return:.2%}, "
                f"annual {evaluation.annualized_return:.1%}) -> {decision}"
            )
        return lines

    def render_manual_review_lines(
        self,
        limit: int,
        sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
    ) -> list[str]:
        if limit <= 0:
            return []
        pairs = sort_manual_review_pairs(self.manual_review_pairs, sort)[:limit]
        if not pairs:
            return ["manual review: none"]
        lines = [f"manual review (top {len(pairs)}; NOT TRADE SAFE):"]
        for index, pair in enumerate(pairs, start=1):
            kalshi = pair.metadata_excerpts.get("kalshi", {})
            poly = pair.metadata_excerpts.get("polymarket", {})
            mismatches = "; ".join(str(value) for value in pair.differing_fields.values())
            kalshi_type = pair.matched_fields.get("kalshi_market_type")
            poly_type = pair.matched_fields.get("poly_market_type")
            lines.extend(
                [
                    f"[{index}] NOT TRADE SAFE | similarity={pair.confidence:.4f}",
                    f"  Kalshi: {pair.kalshi_ticker} | {pair.kalshi_title}",
                    f"  Polymarket: {pair.poly_condition_id} | {pair.poly_question}",
                    f"  matched tokens: {', '.join(pair.matched_tokens) or 'none'}",
                    f"  market types: Kalshi={kalshi_type} | Polymarket={poly_type}",
                    (
                        "  event dates: Kalshi="
                        f"{pair.matched_fields.get('kalshi_event_date')} | Polymarket="
                        f"{pair.matched_fields.get('poly_event_date')}"
                    ),
                    (
                        "  event years: Kalshi="
                        f"{pair.matched_fields.get('kalshi_event_year')} | Polymarket="
                        f"{pair.matched_fields.get('poly_event_year')}"
                    ),
                    (
                        "  thresholds/lines: Kalshi="
                        f"{pair.matched_fields.get('kalshi_threshold')} | Polymarket="
                        f"{pair.matched_fields.get('poly_threshold')}"
                    ),
                    (
                        "  evidence sources: types="
                        f"{pair.matched_fields.get('kalshi_market_type_evidence')} | "
                        f"{pair.matched_fields.get('poly_market_type_evidence')}; dates="
                        f"{pair.matched_fields.get('kalshi_event_date_evidence')} | "
                        f"{pair.matched_fields.get('poly_event_date_evidence')}; thresholds="
                        f"{pair.matched_fields.get('kalshi_threshold_evidence')} | "
                        f"{pair.matched_fields.get('poly_threshold_evidence')}"
                    ),
                    f"  mismatched fields: {mismatches or 'none'}",
                    (f"  missing rule fields: {', '.join(pair.missing_rule_fields) or 'none'}"),
                    (
                        "  rule evidence: Kalshi source="
                        f"{kalshi.get('resolution_source') or 'unknown'} void="
                        f"{kalshi.get('void_policy') or 'unknown'} policies="
                        f"{kalshi.get('sports_policy_terms') or []} | Polymarket source="
                        f"{poly.get('resolution_source') or 'unknown'} void="
                        f"{poly.get('void_policy') or 'unknown'} policies="
                        f"{poly.get('sports_policy_terms') or []} dispute="
                        f"{poly.get('dispute_terms') or []}"
                    ),
                    (
                        "  rule excerpts: Kalshi="
                        f"{_text_excerpt(kalshi.get('rules_text'))} | Polymarket="
                        f"{_text_excerpt(poly.get('description'))}"
                    ),
                    f"  reasons: {'; '.join(pair.status_reasons)}",
                    (
                        "  times: Kalshi close="
                        f"{kalshi.get('close_time')} determination="
                        f"{kalshi.get('determination_time')} | Polymarket end="
                        f"{poly.get('end_time')} resolution={poly.get('resolution_time')}"
                    ),
                    f"  fee confidence: {pair.fee_confidence}",
                    (
                        "  hypothetical edge: NOT COMPUTED - NOT TRADE SAFE"
                        if pair.hypothetical_economics is None
                        else f"  hypothetical edge: {pair.hypothetical_economics} - NOT TRADE SAFE"
                    ),
                ]
            )
        return lines


def hold_days_from_markets(
    kalshi_market: dict[str, Any],
    poly_market: dict[str, Any] | PolymarketMarket,
    now: datetime,
) -> Decimal | None:
    kalshi_end = determination_time(kalshi_market, polymarket=False)
    poly_end = determination_time(poly_market, polymarket=True)
    if kalshi_end is None or poly_end is None:
        return None
    end = max(kalshi_end, poly_end)
    seconds = Decimal(str((end - now).total_seconds()))
    if seconds <= 0:
        return None
    return seconds / Decimal(86_400)


def quote_age_seconds(books: tuple[OrderBook, ...], now: datetime) -> float | None:
    timestamps = [book.timestamp_ms for book in books]
    if any(timestamp is None for timestamp in timestamps):
        return None
    oldest_ms = min(timestamp for timestamp in timestamps if timestamp is not None)
    return max(0.0, now.timestamp() - oldest_ms / 1000)


def _market_category(market: dict[str, Any] | PolymarketMarket) -> str | None:
    if isinstance(market, PolymarketMarket):
        return market.category
    category = market.get("category")
    if category:
        return str(category).lower()
    for tag in market.get("tags") or []:
        if isinstance(tag, str):
            return tag.lower()
        if isinstance(tag, dict):
            value = tag.get("label") or tag.get("name") or tag.get("slug")
            if value:
                return str(value).lower()
    return None


def _poly_candidate_index(
    markets: list[PolymarketMarket],
) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for position, market in enumerate(markets):
        for token in parse_features(market.question, reference_time=market.end_time).tokens:
            index.setdefault(token, set()).add(position)
    return index


def _candidate_positions(
    kalshi_market: dict[str, Any],
    poly_index: dict[str, set[int]],
    poly_market_count: int,
) -> set[int]:
    evidence: Counter[int] = Counter()
    rare_evidence: Counter[int] = Counter()
    # Token-breadth caps must scale with corpus size: with an absolute cap,
    # growing the window pushes previously-rare tokens over the limit and
    # silently REMOVES candidate pairs (observed at 10k markets on
    # 2026-06-11: structured pairs fell from ~1.3k to ~0.4k and whole
    # verified conflict families vanished from the funnel). Behavior at
    # <=5,000 markets is unchanged; larger windows scale the caps
    # proportionally. This is a recall fix only — every additional pair
    # still flows through the same conservative rule gates.
    broad_limit = max(10, min(max(50, poly_market_count // 100), poly_market_count // 20))
    rare_limit = max(2, min(max(10, poly_market_count // 500), poly_market_count // 100))
    # Categorical Kalshi markets carry a generic title; the per-contract
    # outcome entity (candidate, participant) lives in custom_strike /
    # yes_sub_title. Including those tokens is recall-only: it lets the
    # entity-aligned Polymarket market pair up, and every formed pair still
    # passes the unchanged conservative validation.
    searchable = str(kalshi_market.get("title") or "")
    entity = kalshi_outcome_entity(kalshi_market)
    if entity is not None:
        searchable = f"{searchable} {entity.value}"
    for token in parse_features(searchable).tokens:
        positions = poly_index.get(token)
        if not positions or len(positions) > broad_limit:
            continue
        for position in positions:
            evidence[position] += 1
            if len(positions) <= rare_limit:
                rare_evidence[position] += 1
    if not evidence:
        return set()
    eligible = [
        position
        for position, count in evidence.items()
        if (count >= 2 and (rare_evidence[position] >= 1 or count >= 3))
    ]
    ranked = sorted(
        eligible,
        key=lambda position: (
            -evidence[position],
            -rare_evidence[position],
            position,
        ),
    )
    return set(ranked[:8])


def _rejection_bucket(reason: str) -> str:
    lowered = reason.lower()
    for label, fragments in (
        # Named text-evidence conflicts first: their messages can contain
        # generic words ("threshold") that would otherwise mis-bucket them.
        ("continent_scope_conflict", ("continent_scope_conflict",)),
        ("sports_stage_vs_winner_conflict", ("sports_stage_vs_winner_conflict",)),
        (
            "crypto_performance_vs_price_threshold_conflict",
            ("crypto_performance_vs_price_threshold_conflict",),
        ),
        (
            "stock_close_vs_intramonth_high_conflict",
            ("stock_close_vs_intramonth_high_conflict",),
        ),
        ("similarity_below_review_threshold", ("similarity below",)),
        ("determination_time_conflict", ("determination_time", "determination time differs")),
        ("event_date_conflict", ("event_date",)),
        # Market type before threshold: type names can contain "threshold"
        # ("market type crypto_price_threshold (title) != …") while genuine
        # threshold reasons never contain "market type".
        ("market_type_conflict", ("market type",)),
        ("threshold_conflict", ("strike ", "threshold ")),
        ("contract_shape_conflict", ("contract shape",)),
        ("settlement_basis_conflict", ("settlement_basis_conflict",)),
        ("office_level_conflict", ("office_level_conflict",)),
        ("basket_scope_conflict", ("basket_scope_conflict",)),
        ("candidate_set_conflict", ("candidate_set_conflict",)),
        ("player_prop_scope_conflict", ("player_prop_scope_conflict",)),
        ("central_bank_direction_conflict", ("central_bank_direction_conflict",)),
        ("outcome_entity_conflict", ("outcome party", "outcome entity")),
        ("resolution_source_conflict", ("resolution source",)),
        ("void_policy_conflict", ("void policy", "void_policy_conflict")),
        ("sports_policy_conflict", ("sports postponement",)),
    ):
        if any(fragment in lowered for fragment in fragments):
            return label
    return "other_rule_conflict"


def _primary_rejection_bucket(reasons: tuple[str, ...]) -> str:
    priority = {
        "determination_time_conflict": 0,
        "event_date_conflict": 1,
        # Named text-evidence conflicts outrank the generic threshold and
        # market-type buckets so the histogram reports the most specific
        # verified reason.
        "continent_scope_conflict": 2,
        "sports_stage_vs_winner_conflict": 3,
        "crypto_performance_vs_price_threshold_conflict": 4,
        "stock_close_vs_intramonth_high_conflict": 5,
        "threshold_conflict": 6,
        "market_type_conflict": 7,
        "contract_shape_conflict": 8,
        "settlement_basis_conflict": 9,
        "office_level_conflict": 10,
        "basket_scope_conflict": 11,
        "candidate_set_conflict": 12,
        "player_prop_scope_conflict": 13,
        "central_bank_direction_conflict": 14,
        "outcome_entity_conflict": 15,
        "resolution_source_conflict": 16,
        "void_policy_conflict": 17,
        "sports_policy_conflict": 18,
        "similarity_below_review_threshold": 19,
        "other_rule_conflict": 20,
    }
    buckets = {_rejection_bucket(reason) for reason in reasons}
    return min(buckets, key=priority.__getitem__)


async def _resolve_poly_fee_schedule(
    clob: ClobClientProto,
    poly_market: PolymarketMarket,
    category: str | None,
) -> tuple[FeeSchedule, bool]:
    market_schedule = poly_market.fee_schedule
    if market_schedule is None:
        try:
            market_info = await clob.get_market_info(poly_market.condition_id)
        except Exception as exc:
            log.warning("Polymarket fee metadata lookup failed (%s)", type(exc).__name__)
        else:
            market_schedule = fee_schedule_from_metadata(market_info)
    resolved = resolve_fee_schedule(market_schedule, category)
    return resolved, resolved.source is FeeRateSource.MARKET_METADATA


def _paired_snapshot(
    books: tuple[OrderBook, OrderBook, OrderBook, OrderBook],
    snapshot_ids: dict[str, int],
) -> dict[str, Any]:
    kalshi_yes, kalshi_no, poly_yes, poly_no = books
    return {
        "format": "paired_opportunity",
        "kalshi_yes": orderbook_snapshot_payload(kalshi_yes),
        "kalshi_no": orderbook_snapshot_payload(kalshi_no),
        "poly_yes": orderbook_snapshot_payload(poly_yes),
        "poly_no": orderbook_snapshot_payload(poly_no),
        "snapshot_ids": snapshot_ids,
    }


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
    cost_assumptions: CostAssumptions | None = None,
    allow_unknown_fees: bool = False,
    allow_unknown_costs: bool = False,
    store: ScanStoreProto | None = None,
    size: int = 100,
    polymarket_page_size: int = 100,
    polymarket_max_pages: int = 5,
    polymarket_max_markets: int = 500,
    kalshi_page_limit: int = 1000,
    max_kalshi_pages: int = 100,
    min_similarity_prefilter: float = 0.35,
    now: datetime | None = None,
) -> ScanReport:
    limits = limits or RiskLimits()
    exposure = exposure or ExposureTracker()
    kill_switch = kill_switch or KillSwitch()
    slippage_model = slippage_model or FixedCentsSlippage(cents_per_share=Decimal("0.5"))
    costs = cost_assumptions or CostAssumptions()
    fixed_costs = costs.fee_breakdown()
    missing_costs = costs.missing_components()
    scan_time = now or datetime.now(UTC)
    report = ScanReport()
    scan_id = uuid4().hex

    raw_kalshi_markets = await kalshi.get_all_markets(
        limit=kalshi_page_limit,
        status="open",
        mve_filter="exclude",
        max_pages=max_kalshi_pages,
    )
    kalshi_markets = [m for m in raw_kalshi_markets if kalshi_is_scannable(m)]
    gamma_result = await gamma.get_all_markets(
        page_size=polymarket_page_size,
        max_pages=polymarket_max_pages,
        max_markets=polymarket_max_markets,
        closed=False,
    )
    raw_poly_markets = gamma_result.markets
    normalized_poly_markets = [PolymarketMarket.from_gamma(market) for market in raw_poly_markets]
    poly_markets = [market for market in normalized_poly_markets if market.scannable]
    report.kalshi_markets_discovered = len(raw_kalshi_markets)
    report.kalshi_markets_scannable = len(kalshi_markets)
    report.poly_markets_discovered = len(normalized_poly_markets)
    report.poly_markets_scannable = len(poly_markets)
    report.poly_pages_fetched = gamma_result.pages_fetched
    report.poly_rows_fetched = gamma_result.total_fetched
    poly_index = _poly_candidate_index(poly_markets)

    for k_market in kalshi_markets:
        candidate_positions = _candidate_positions(k_market, poly_index, len(poly_markets))
        report.raw_title_candidates += len(candidate_positions)
        for position in sorted(candidate_positions):
            p_market = poly_markets[position]
            pair = evaluate_pair(
                k_market,
                p_market,
                min_rule_review_score=min_similarity_prefilter,
            )
            if pair is None:
                continue
            pair = replace(pair, scan_id=scan_id)
            pair_id = await store.add_pair(pair) if store is not None else None
            if pair.confidence >= min_similarity_prefilter:
                report.structured_candidates += 1
                report.pairs_considered += 1
            if pair.status is MatchStatus.MANUAL_REVIEW:
                report.manual_review_candidates += 1
                report.manual_review_pairs.append(pair)
                continue
            if pair.status is MatchStatus.REJECTED:
                report.rejected_candidates += 1
                report.rejected_pairs.append(pair)
                bucket = _primary_rejection_bucket(
                    pair.status_reasons or ("unspecified rejection",)
                )
                report.rejection_reasons[bucket] += 1
                continue
            report.pairs_accepted += 1

            kalshi_payload = await kalshi.get_orderbook(pair.kalshi_ticker)
            captured_ms = int(scan_time.timestamp() * 1000)
            kalshi_book = KalshiBook.from_rest_payload(
                pair.kalshi_ticker, kalshi_payload, timestamp_ms=captured_ms
            )
            kalshi_yes = kalshi_book.view(Side.YES)
            kalshi_no = kalshi_book.view(Side.NO)
            poly_yes = from_clob_payload(await clob.get_book(pair.poly_yes_token_id), Side.YES)
            poly_no = from_clob_payload(await clob.get_book(pair.poly_no_token_id), Side.NO)
            books = (kalshi_yes, kalshi_no, poly_yes, poly_no)

            category = _market_category(p_market)
            fee_schedule, fee_metadata_known = await _resolve_poly_fee_schedule(
                clob, p_market, category
            )
            hold_days = hold_days_from_markets(k_market, p_market, scan_time)
            quote_age = quote_age_seconds(books, scan_time)
            economics_hold_days = hold_days or Decimal(365)

            snapshot_ids: dict[str, int] = {}
            if store is not None:
                for name, book in zip(
                    ("kalshi_yes", "kalshi_no", "poly_yes", "poly_no"), books, strict=True
                ):
                    snapshot_ids[name] = await store.add_snapshot(
                        book.venue.value, book.market_id, orderbook_snapshot_payload(book)
                    )
            paired_snapshot = _paired_snapshot(books, snapshot_ids)

            evaluations = evaluate_both_directions(
                kalshi_yes_view=kalshi_yes,
                kalshi_no_view=kalshi_no,
                poly_yes_book=poly_yes,
                poly_no_book=poly_no,
                size=size,
                poly_fee_schedule=fee_schedule,
                slippage_model=slippage_model,
                fixed_costs=fixed_costs,
                hold_days=economics_hold_days,
            )
            for evaluation in evaluations:
                gross_edge_per_share = (
                    evaluation.gross.to_dollars() / Decimal(evaluation.executable_size)
                    if evaluation.executable_size
                    else Decimal(0)
                )
                reasons = check(
                    OpportunityRisk(
                        locked_capital=evaluation.locked,
                        net_profit=evaluation.net,
                        simple_return=evaluation.simple_return,
                        annualized_return=evaluation.annualized_return,
                        match_confidence=pair.confidence,
                        fill_fraction=evaluation.fill_fraction,
                        hold_days=hold_days,
                        quote_age_seconds=quote_age,
                        category=category,
                        gross_edge_per_share=gross_edge_per_share,
                    ),
                    limits,
                    exposure,
                    kill_switch,
                )
                if not fee_metadata_known and not allow_unknown_fees:
                    reasons.append(f"Polymarket fee metadata {fee_schedule.source.value}")
                if missing_costs and not allow_unknown_costs:
                    reasons.append(f"unknown required costs: {', '.join(missing_costs)}")
                report.opportunities.append((pair, evaluation, reasons))

                payload: AlertPayload | None = None
                verification_verdict: str | None = None
                unresolved_flags: tuple[str, ...] = ()
                if not reasons:
                    # Verify risk flags using the automated verifier
                    kalshi_excerpt = pair.metadata_excerpts["kalshi"]
                    poly_excerpt = pair.metadata_excerpts["polymarket"]

                    # Parse determination times from ISO strings
                    kalshi_time = None
                    if kalshi_excerpt.get("determination_time"):
                        try:
                            kalshi_time = datetime.fromisoformat(
                                kalshi_excerpt["determination_time"].replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass

                    poly_time = None
                    poly_resolution_time = poly_excerpt.get("resolution_time")
                    poly_end_time = poly_excerpt.get("end_time")
                    if poly_resolution_time:
                        try:
                            poly_time = datetime.fromisoformat(
                                poly_resolution_time.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass
                    elif poly_end_time:
                        try:
                            poly_time = datetime.fromisoformat(
                                poly_end_time.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass

                    inputs = VerificationInputs(
                        kalshi_rules_text=kalshi_excerpt.get("rules_text", ""),
                        poly_rules_text=poly_excerpt.get("description", ""),
                        kalshi_determination_time=kalshi_time,
                        poly_determination_time=poly_time,
                    )

                    verification_report = verify_pair(pair.rule_warnings, inputs)
                    verification_verdict = verification_report.verdict.value
                    unresolved_flags = verification_report.unresolved()

                    # Update verification statistics
                    report.verification_verdicts[verification_verdict] += 1

                    # If verifier rejects the pair, don't create an alert
                    if verification_verdict == VerificationVerdict.REJECTED.value:
                        reasons.append("verifier rejected: normal-state divergence proven")
                        payload = None
                    else:
                        payload = AlertPayload(
                            kalshi_ticker=pair.kalshi_ticker,
                            poly_condition_id=pair.poly_condition_id,
                            direction=evaluation.direction.value,
                            confidence=pair.confidence,
                            size=evaluation.executable_size,
                            depth_summary=(
                                f"requested={evaluation.requested_size} "
                                f"executable={evaluation.executable_size} "
                                f"fill={evaluation.fill_fraction:.2%} "
                                f"k_levels={evaluation.kalshi_leg.levels_consumed} "
                                f"p_levels={evaluation.poly_leg.levels_consumed}"
                            ),
                            fees=evaluation.fees,
                            net_edge=evaluation.net,
                            simple_return=evaluation.simple_return,
                            annualized_return=evaluation.annualized_return,
                            break_even_slippage_per_share=evaluation.break_even_slippage_per_share,
                            break_even_extra_fees=evaluation.break_even_extra_fees,
                            snapshot_id=snapshot_ids.get("kalshi_yes"),
                            risk_flags=pair.rule_warnings,
                            verification_verdict=verification_verdict,
                            unresolved_flags=unresolved_flags,
                        )

                assumptions: dict[str, Any] = {
                    "requested_size": evaluation.requested_size,
                    "executable_size": evaluation.executable_size,
                    "fill_fraction": str(evaluation.fill_fraction),
                    "hold_days": str(hold_days) if hold_days is not None else None,
                    "quote_age_seconds": quote_age,
                    "polymarket_fee_rate": str(fee_schedule.rate),
                    "polymarket_fee_exponent": str(fee_schedule.exponent),
                    "polymarket_fee_source": fee_schedule.source.value,
                    "missing_costs": list(missing_costs),
                    "kalshi_fee_source": "general_schedule_conservative",
                    "risk_flags": list(pair.rule_warnings),
                    "verification_verdict": verification_verdict,
                    "unresolved_flags": list(unresolved_flags),
                    "alert_payload": payload.to_dict() if payload is not None else None,
                }
                if store is not None:
                    assert pair_id is not None
                    await store.add_opportunity(
                        pair_id=pair_id,
                        evaluation=evaluation,
                        decision="alerted" if payload is not None else "rejected",
                        rejection_reason="; ".join(reasons) if reasons else None,
                        assumptions=assumptions,
                        paired_snapshot=paired_snapshot,
                    )

                if payload is not None:
                    for sink in sinks:
                        try:
                            await sink.send(payload)
                        except Exception:
                            log.exception("alert sink %r failed", type(sink).__name__)
                    # Discovery mode never places orders. This conservative reservation
                    # only prevents repeated scan-session alerts from ignoring exposure.
                    exposure.add(Venue.KALSHI, evaluation.locked)
                    exposure.add(Venue.POLYMARKET, evaluation.locked)

    return report
