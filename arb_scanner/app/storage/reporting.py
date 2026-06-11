"""Human-readable reports for persisted matching diagnostics."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from arb_scanner.app.markets.discovery import ManualReviewSort, diagnostic_sort_key
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.models import MatchedPairRow
from arb_scanner.app.storage.repo import PairRepo


def _details(row: MatchedPairRow) -> dict[str, Any]:
    return row.matched_fields


def _text_excerpt(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or "missing"


def render_pair_row(row: MatchedPairRow) -> list[str]:
    details = _details(row)
    excerpts = details.get("metadata_excerpts") or {}
    kalshi = excerpts.get("kalshi") or {}
    poly = excerpts.get("polymarket") or {}
    reasons = details.get("status_reasons") or row.rule_warnings or []
    missing = details.get("missing_rule_fields") or []
    matched_tokens = details.get("matched_tokens") or []
    mismatches = "; ".join(str(value) for value in row.differing_fields.values())
    hypothetical = details.get("hypothetical_economics")
    kalshi_type = details.get("kalshi_market_type")
    poly_type = details.get("poly_market_type")
    return [
        f"{row.status.upper()} | confidence={row.confidence:.4f} | NOT TRADE SAFE"
        if row.status != "accepted"
        else f"ACCEPTED | confidence={row.confidence:.4f}",
        f"  Kalshi: {row.kalshi_ticker} | {details.get('kalshi_title', '')}",
        f"  Polymarket: {row.poly_condition_id} | {details.get('poly_question', '')}",
        f"  matched tokens: {', '.join(str(item) for item in matched_tokens) or 'none'}",
        f"  market types: Kalshi={kalshi_type} | Polymarket={poly_type}",
        (
            f"  event dates: Kalshi={details.get('kalshi_event_date')} | "
            f"Polymarket={details.get('poly_event_date')}"
        ),
        (
            f"  event years: Kalshi={details.get('kalshi_event_year')} | "
            f"Polymarket={details.get('poly_event_year')}"
        ),
        (
            f"  thresholds/lines: Kalshi={details.get('kalshi_threshold')} | "
            f"Polymarket={details.get('poly_threshold')}"
        ),
        (
            f"  evidence sources: types={details.get('kalshi_market_type_evidence')} | "
            f"{details.get('poly_market_type_evidence')}; "
            f"dates={details.get('kalshi_event_date_evidence')} | "
            f"{details.get('poly_event_date_evidence')}; "
            f"thresholds={details.get('kalshi_threshold_evidence')} | "
            f"{details.get('poly_threshold_evidence')}"
        ),
        f"  mismatched fields: {mismatches or 'none'}",
        f"  missing rule fields: {', '.join(str(item) for item in missing) or 'none'}",
        f"  reasons: {'; '.join(str(item) for item in reasons) or 'none'}",
        (
            f"  times: Kalshi close={kalshi.get('close_time')} "
            f"determination={kalshi.get('determination_time')} | "
            f"Polymarket end={poly.get('end_time')} resolution={poly.get('resolution_time')}"
        ),
        (
            f"  rule evidence: Kalshi source={kalshi.get('resolution_source') or 'unknown'} "
            f"void={kalshi.get('void_policy') or 'unknown'} "
            f"policies={kalshi.get('sports_policy_terms') or []} | "
            f"Polymarket source={poly.get('resolution_source') or 'unknown'} "
            f"void={poly.get('void_policy') or 'unknown'} "
            f"policies={poly.get('sports_policy_terms') or []} "
            f"dispute={poly.get('dispute_terms') or []}"
        ),
        (
            f"  rule excerpts: Kalshi={_text_excerpt(kalshi.get('rules_text'))} | "
            f"Polymarket={_text_excerpt(poly.get('description'))}"
        ),
        f"  fee confidence: {details.get('fee_confidence', 'unknown')}",
        (
            "  hypothetical edge: NOT COMPUTED - NOT TRADE SAFE"
            if hypothetical is None
            else f"  hypothetical edge: {hypothetical} - NOT TRADE SAFE"
        ),
    ]


async def load_diagnostic_rows(
    database_url: str,
    *,
    mode: str,
    limit: int,
    sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
) -> list[MatchedPairRow]:
    engine = make_engine(database_url)
    await init_models(engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            repo = PairRepo(session)
            if mode == "latest":
                return await repo.list_latest_scan(limit=limit)
            if mode == "manual_review":
                rows = await repo.list_latest_scan(
                    status="manual_review", limit=max(1000, limit * 100)
                )
                return sorted(
                    rows,
                    key=lambda row: diagnostic_sort_key(
                        mode=sort,
                        confidence=row.confidence,
                        missing_fields=row.matched_fields.get("missing_rule_fields") or [],
                        category=row.matched_fields.get("category"),
                        event_dates=(
                            row.matched_fields.get("kalshi_event_date"),
                            row.matched_fields.get("poly_event_date"),
                        ),
                        hypothetical_economics=row.matched_fields.get("hypothetical_economics"),
                    ),
                )[:limit]
            if mode == "rejected":
                return await repo.list_latest_scan(status="rejected", limit=limit)
            raise ValueError(f"unknown diagnostic report mode {mode!r}")
    finally:
        await engine.dispose()


def run_diagnostic_report(
    database_url: str,
    *,
    mode: str,
    limit: int,
    sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
) -> int:
    rows = asyncio.run(load_diagnostic_rows(database_url, mode=mode, limit=limit, sort=sort))
    if not rows:
        print(f"no persisted {mode.replace('_', '-')} candidates")
        return 1
    statuses = Counter(row.status for row in rows)
    print(
        f"persisted candidate report: mode={mode} sort={sort.value} rows={len(rows)} "
        + " ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
    )
    for row in rows:
        for line in render_pair_row(row):
            print(line)
    return 0
