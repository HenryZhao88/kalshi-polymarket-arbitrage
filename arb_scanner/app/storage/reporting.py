"""Human-readable and machine-readable reports for persisted diagnostics.

All report modes (text, csv, json, verification packet) render the same
sorted record order, are labeled NOT TRADE SAFE for non-accepted rows, and
never claim arbitrage or profitability.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from arb_scanner.app.markets.discovery import ManualReviewSort, diagnostic_sort_key
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.export import (
    blocking_summary_lines,
    pair_record,
    render_csv,
    render_json,
    render_verification_packet,
)
from arb_scanner.app.storage.models import BookSnapshotRow, MatchedPairRow, OpportunityRow
from arb_scanner.app.storage.repo import PairRepo, SqlAlchemyScanStore

REPORT_FORMATS: tuple[str, ...] = ("text", "csv", "json")


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
    record = pair_record(row)
    return [
        f"{row.status.upper()} | confidence={row.confidence:.4f} | NOT TRADE SAFE"
        if row.status != "accepted"
        else f"ACCEPTED | confidence={row.confidence:.4f}",
        f"  Kalshi: {row.kalshi_ticker} | {details.get('kalshi_title', '')}",
        f"  Polymarket: {row.poly_condition_id} | {details.get('poly_question', '')}",
        *blocking_summary_lines(record),
        (
            f"  source ids: Kalshi event={record['kalshi_event_ticker'] or 'unknown'} | "
            f"Polymarket slug={record['polymarket_slug'] or 'unknown'} "
            f"url={record['polymarket_url'] or 'not derivable'}"
        ),
        (
            f"  outcome entities: Kalshi={record['kalshi_outcome_entity'] or 'unknown'} | "
            f"Polymarket={record['polymarket_outcome_entity'] or 'unknown'}"
        ),
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
        (
            "  verify manually: "
            + (
                ", ".join(name for name in record if name.startswith("needs_") and record[name])
                or "nothing flagged"
            )
        ),
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


def _row_sort_key(row: MatchedPairRow, sort: ManualReviewSort) -> tuple[Any, ...]:
    details = row.matched_fields
    return diagnostic_sort_key(
        mode=sort,
        confidence=row.confidence,
        missing_fields=details.get("missing_rule_fields") or [],
        category=details.get("category"),
        event_dates=(
            details.get("kalshi_event_date"),
            details.get("poly_event_date"),
        ),
        hypothetical_economics=details.get("hypothetical_economics"),
        market_type=details.get("kalshi_market_type") or details.get("poly_market_type"),
        fee_confidence=details.get("fee_confidence"),
    )


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
            if mode in ("manual_review", "rejected"):
                status = "manual_review" if mode == "manual_review" else "rejected"
                rows = await repo.list_latest_scan(status=status, limit=max(1000, limit * 100))
                # sorted() is stable, so equal keys keep recency order.
                return sorted(rows, key=lambda row: _row_sort_key(row, sort))[:limit]
            raise ValueError(f"unknown diagnostic report mode {mode!r}")
    finally:
        await engine.dispose()


def _render_report(
    rows: list[MatchedPairRow],
    *,
    mode: str,
    sort: ManualReviewSort,
    fmt: str,
    verification_packet: bool,
) -> str:
    records = [pair_record(row) for row in rows]
    if verification_packet:
        return "\n".join(render_verification_packet(records)) + "\n"
    if fmt == "csv":
        return render_csv(records)
    if fmt == "json":
        return render_json(records, mode=mode, sort=sort.value)
    statuses = Counter(row.status for row in rows)
    lines = [
        f"persisted candidate report: mode={mode} sort={sort.value} rows={len(rows)} "
        + " ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
    ]
    for row in rows:
        lines.extend(render_pair_row(row))
    return "\n".join(lines) + "\n"


def run_diagnostic_report(
    database_url: str,
    *,
    mode: str,
    limit: int,
    sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
    fmt: str = "text",
    output: str | None = None,
    verification_packet: bool = False,
) -> int:
    if fmt not in REPORT_FORMATS:
        raise ValueError(f"unknown report format {fmt!r}")
    rows = asyncio.run(load_diagnostic_rows(database_url, mode=mode, limit=limit, sort=sort))
    if not rows:
        print(f"no persisted {mode.replace('_', '-')} candidates")
        return 1
    rendered = _render_report(
        rows, mode=mode, sort=sort, fmt=fmt, verification_packet=verification_packet
    )
    if output is None:
        print(rendered, end="")
        return 0
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    kind = "verification-packet" if verification_packet else fmt
    print(f"wrote {len(rows)} {mode.replace('_', '-')} row(s) to {path} (format={kind})")
    return 0


async def _cleanup_retention(database_url: str, *, retention_days: int) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    engine = make_engine(database_url)
    await init_models(engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            counts: dict[str, int] = {}
            for name, model, column in (
                ("matched_pairs", MatchedPairRow, MatchedPairRow.created_at),
                ("book_snapshots", BookSnapshotRow, BookSnapshotRow.captured_at),
                ("opportunities", OpportunityRow, OpportunityRow.created_at),
            ):
                result = await session.execute(
                    select(func.count()).select_from(model).where(column < cutoff)
                )
                counts[name] = int(result.scalar_one())
            store = SqlAlchemyScanStore(session)
            await store.apply_retention(cutoff)
            await session.commit()
            return counts
    finally:
        await engine.dispose()


def run_retention_cleanup(database_url: str, *, retention_days: int) -> int:
    """Delete rows older than the retention window and report what was removed."""
    counts = asyncio.run(_cleanup_retention(database_url, retention_days=retention_days))
    summary = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    print(f"retention cleanup: removed rows older than {retention_days}d -> {summary}")
    return 0
