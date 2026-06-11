"""Research-grade exports of persisted match diagnostics (CSV/JSON/packet).

Everything rendered here is diagnostic material for HUMAN verification. Every
exported row carries the NOT TRADE SAFE label regardless of status: this
scanner is discovery-only, manual-review rows have unverified rule
equivalence, and nothing in an export is a trade recommendation or a claim of
profitable arbitrage.

Exports only contain venue market metadata already persisted by the scanner —
never settings, credentials, or other secret-bearing values.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from arb_scanner.app.storage.models import MatchedPairRow

NOT_TRADE_SAFE_LABEL = "NOT TRADE SAFE"

PACKET_DISCLAIMER = (
    "Research material only. No row is a trade recommendation, and no claim "
    "of arbitrage or profitability is made. Verify every checklist item "
    "against official venue rules before drawing any conclusion."
)

#: Public Polymarket event page path. Gamma event metadata exposes `slug`
#: values that map to https://polymarket.com/event/<event-slug>. A market-level
#: slug is exported as an identifier only — its URL mapping is not derived
#: here. No documented public Kalshi URL is derivable from a market ticker
#: alone, so Kalshi rows export identifiers (ticker, event ticker) without a
#: URL (docs/VERIFICATION.md).
_POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"

CHECKLIST_FIELDS: tuple[str, ...] = (
    "needs_determination_time",
    "needs_resolution_source",
    "needs_void_policy",
    "needs_threshold_confirmation",
    "needs_event_date_confirmation",
    "needs_market_type_confirmation",
    "needs_fee_confirmation",
    "needs_liquidity_confirmation",
)

_CHECKLIST_LABELS: dict[str, str] = {
    "needs_determination_time": "determination time verified on both venues",
    "needs_resolution_source": "official resolution source verified on both venues",
    "needs_void_policy": "void/refund policy verified on both venues",
    "needs_threshold_confirmation": "threshold/strike equivalence confirmed",
    "needs_event_date_confirmation": "event date equivalence confirmed",
    "needs_market_type_confirmation": "market type equivalence confirmed",
    "needs_fee_confirmation": "fee schedule confirmed from venue metadata",
    "needs_liquidity_confirmation": "order-book liquidity reviewed (economics not computed)",
}

#: Stable CSV header order. Append-only: downstream spreadsheets key on these.
EXPORT_FIELDS: tuple[str, ...] = (
    "status",
    "confidence",
    "reason",
    "not_trade_safe_label",
    "fee_confidence",
    "hypothetical_edge_status",
    "kalshi_ticker",
    "kalshi_event_ticker",
    "kalshi_title",
    "kalshi_market_type",
    "kalshi_event_date",
    "kalshi_threshold",
    "kalshi_close_time",
    "kalshi_determination_time",
    "kalshi_resolution_source",
    "kalshi_void_policy",
    "polymarket_condition_id",
    "polymarket_slug",
    "polymarket_title",
    "polymarket_token_ids",
    "polymarket_market_type",
    "polymarket_event_date",
    "polymarket_threshold",
    "polymarket_end_time",
    "polymarket_resolution_time",
    "polymarket_resolution_source",
    "polymarket_void_policy",
    "polymarket_url",
    "missing_fields",
    "conflicting_fields",
    "matched_tokens",
    "mismatched_fields",
    "rule_evidence_summary",
    "kalshi_rules_excerpt",
    "polymarket_rules_excerpt",
    "unsafe_hypothetical_edge_if_available",
    *CHECKLIST_FIELDS,
    # Appended after the checklist to honor the append-only header contract.
    "kalshi_cancellation_policy_basis",
    "polymarket_cancellation_policy_basis",
)


def polymarket_public_url(event_slug: str | None) -> str | None:
    if not event_slug:
        return None
    return _POLYMARKET_EVENT_URL.format(slug=event_slug)


def _is_missing(value: Any) -> bool:
    return value in (None, "")


def _needs_verification(
    field_name: str,
    left: Any,
    right: Any,
    *,
    missing: set[str],
    conflict_text: str,
    conflict_phrases: tuple[str, ...],
    compare_values: bool = False,
) -> bool:
    """True when a checklist field still needs human verification.

    Missing on either venue, named in the unresolved rule fields, or named in
    a structured conflict all flag the field. `compare_values` additionally
    flags venue values that are both present but unequal (only meaningful for
    fields that must be identical across venues, e.g. thresholds — venue
    clocks like determination times legitimately differ).
    """
    if field_name in missing:
        return True
    if _is_missing(left) or _is_missing(right):
        return True
    if any(phrase in conflict_text for phrase in conflict_phrases):
        return True
    return compare_values and str(left) != str(right)


def verification_checklist(record: dict[str, Any]) -> dict[str, bool]:
    """Diagnostic needs_* booleans for one export record.

    These are NOT acceptance rules: they never accept or reject a pair, they
    only enumerate what a human must verify before trusting a match.
    """
    missing = {str(item) for item in record.get("missing_fields") or []}
    conflict_text = " | ".join(
        str(item) for item in record.get("conflicting_fields") or []
    ).lower()
    poly_determination = record.get("polymarket_resolution_time") or record.get(
        "polymarket_end_time"
    )
    checklist = {
        "needs_determination_time": _needs_verification(
            "determination_time",
            record.get("kalshi_determination_time"),
            poly_determination,
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("determination",),
        ),
        "needs_resolution_source": _needs_verification(
            "resolution_source",
            record.get("kalshi_resolution_source"),
            record.get("polymarket_resolution_source"),
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("resolution source", "resolution_source"),
        ),
        "needs_void_policy": _needs_verification(
            "void_policy",
            record.get("kalshi_void_policy"),
            record.get("polymarket_void_policy"),
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("void",),
        ),
        "needs_threshold_confirmation": _needs_verification(
            "threshold",
            record.get("kalshi_threshold"),
            record.get("polymarket_threshold"),
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("threshold",),
            compare_values=True,
        ),
        "needs_event_date_confirmation": _needs_verification(
            "event_date",
            record.get("kalshi_event_date"),
            record.get("polymarket_event_date"),
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("event date", "event_date"),
            compare_values=True,
        ),
        "needs_market_type_confirmation": _needs_verification(
            "market_type",
            record.get("kalshi_market_type"),
            record.get("polymarket_market_type"),
            missing=missing,
            conflict_text=conflict_text,
            conflict_phrases=("market type", "market_type"),
            compare_values=True,
        ),
        "needs_fee_confirmation": record.get("fee_confidence") != "market_metadata",
        "needs_liquidity_confirmation": (
            record.get("unsafe_hypothetical_edge_if_available") is None
        ),
    }
    return {name: checklist[name] for name in CHECKLIST_FIELDS}


def _rule_evidence_summary(kalshi: dict[str, Any], poly: dict[str, Any]) -> str:
    return (
        f"kalshi: source={kalshi.get('resolution_source') or 'unverified'}, "
        f"void={kalshi.get('void_policy') or 'unknown'}, "
        f"cancellation={kalshi.get('cancellation_policy_basis') or 'unknown'}, "
        f"policies={list(kalshi.get('sports_policy_terms') or [])}; "
        f"polymarket: source={poly.get('resolution_source') or 'unverified'}, "
        f"void={poly.get('void_policy') or 'unknown'}, "
        f"cancellation={poly.get('cancellation_policy_basis') or 'unknown'}, "
        f"dispute={list(poly.get('dispute_terms') or [])}"
    )


def pair_record(row: MatchedPairRow) -> dict[str, Any]:
    """Flatten one persisted pair into the stable export record.

    Tolerates rows persisted by older scanner versions: any identifier the
    row predates (event ticker, slug) simply exports as None.
    """
    details = row.matched_fields or {}
    excerpts = details.get("metadata_excerpts") or {}
    kalshi = excerpts.get("kalshi") or {}
    poly = excerpts.get("polymarket") or {}
    differing = row.differing_fields or {}
    conflicting = [
        str(value)
        for key, value in differing.items()
        if key.startswith(("conflict_", "rule_"))
    ]
    mismatched = {
        key: value
        for key, value in differing.items()
        if not key.startswith(("conflict_", "rule_"))
    }
    reasons = details.get("status_reasons") or row.rule_warnings or []
    hypothetical = details.get("hypothetical_economics")
    token_ids = [
        token for token in (row.poly_yes_token_id, row.poly_no_token_id) if token
    ] or list(poly.get("token_ids") or [])
    record: dict[str, Any] = {
        "status": row.status,
        "confidence": row.confidence,
        "reason": "; ".join(str(reason) for reason in reasons),
        "not_trade_safe_label": NOT_TRADE_SAFE_LABEL,
        "fee_confidence": details.get("fee_confidence")
        or poly.get("fee_confidence")
        or "unknown",
        "hypothetical_edge_status": (
            "not_computed" if hypothetical is None else "computed_hypothetical_only"
        ),
        "kalshi_ticker": row.kalshi_ticker,
        "kalshi_event_ticker": kalshi.get("event_ticker"),
        "kalshi_title": details.get("kalshi_title") or kalshi.get("title"),
        "kalshi_market_type": details.get("kalshi_market_type"),
        "kalshi_event_date": details.get("kalshi_event_date") or kalshi.get("event_date"),
        "kalshi_threshold": details.get("kalshi_threshold") or kalshi.get("threshold"),
        "kalshi_close_time": kalshi.get("close_time"),
        "kalshi_determination_time": kalshi.get("determination_time"),
        "kalshi_resolution_source": kalshi.get("resolution_source") or None,
        "kalshi_void_policy": kalshi.get("void_policy"),
        "polymarket_condition_id": row.poly_condition_id,
        "polymarket_slug": poly.get("slug"),
        "polymarket_title": details.get("poly_question") or poly.get("question"),
        "polymarket_token_ids": token_ids,
        "polymarket_market_type": details.get("poly_market_type"),
        "polymarket_event_date": details.get("poly_event_date") or poly.get("event_date"),
        "polymarket_threshold": details.get("poly_threshold") or poly.get("threshold"),
        "polymarket_end_time": poly.get("end_time"),
        "polymarket_resolution_time": poly.get("resolution_time"),
        "polymarket_resolution_source": poly.get("resolution_source") or None,
        "polymarket_void_policy": poly.get("void_policy"),
        "polymarket_url": polymarket_public_url(poly.get("event_slug")),
        "missing_fields": [str(item) for item in details.get("missing_rule_fields") or []],
        "conflicting_fields": conflicting,
        "matched_tokens": [str(item) for item in details.get("matched_tokens") or []],
        "mismatched_fields": mismatched,
        "rule_evidence_summary": _rule_evidence_summary(kalshi, poly),
        "kalshi_rules_excerpt": _text_excerpt(kalshi.get("rules_text")),
        "polymarket_rules_excerpt": _text_excerpt(poly.get("description")),
        "unsafe_hypothetical_edge_if_available": hypothetical,
        "kalshi_cancellation_policy_basis": kalshi.get("cancellation_policy_basis"),
        "polymarket_cancellation_policy_basis": poly.get("cancellation_policy_basis"),
    }
    record.update(verification_checklist(record))
    return record


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render_csv(records: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS), lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({field: _csv_cell(record.get(field)) for field in EXPORT_FIELDS})
    return buffer.getvalue()


def render_json(records: list[dict[str, Any]], *, mode: str, sort: str) -> str:
    payload = {
        "label": NOT_TRADE_SAFE_LABEL,
        "disclaimer": PACKET_DISCLAIMER,
        "mode": mode,
        "sort": sort,
        "row_count": len(records),
        "rows": records,
    }
    return json.dumps(payload, indent=2, sort_keys=False, default=str)


def _text_excerpt(value: Any, limit: int = 240) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def render_verification_packet(records: list[dict[str, Any]]) -> list[str]:
    """Human-readable verification packet for the given (pre-sorted) records."""
    lines = [
        f"MANUAL VERIFICATION PACKET — {len(records)} candidate pair(s) — "
        f"ALL ROWS {NOT_TRADE_SAFE_LABEL}",
        PACKET_DISCLAIMER,
        "",
    ]
    for index, record in enumerate(records, start=1):
        unresolved = list(record.get("missing_fields") or [])
        conflicts = list(record.get("conflicting_fields") or [])
        lines.extend(
            [
                (
                    f"[{index}/{len(records)}] {NOT_TRADE_SAFE_LABEL} | "
                    f"status={record.get('status')} | "
                    f"similarity={record.get('confidence'):.4f}"
                ),
                (
                    f"  pair: Kalshi {record.get('kalshi_ticker')} <-> "
                    f"Polymarket {record.get('polymarket_condition_id')}"
                ),
                f"  Kalshi title:     {record.get('kalshi_title')}",
                f"  Polymarket title: {record.get('polymarket_title')}",
                (
                    "  why it matched: market types "
                    f"Kalshi={record.get('kalshi_market_type')} / "
                    f"Polymarket={record.get('polymarket_market_type')}; shared tokens: "
                    f"{', '.join(record.get('matched_tokens') or []) or 'none'}"
                ),
                f"  why not accepted: {record.get('reason') or 'unspecified'}",
                (
                    "  unresolved fields blocking acceptance: "
                    f"{', '.join(unresolved) or 'none recorded'}"
                ),
                f"  structured conflicts: {'; '.join(conflicts) or 'none'}",
                "  identifiers:",
                (
                    f"    kalshi ticker: {record.get('kalshi_ticker')} | "
                    f"event ticker: {record.get('kalshi_event_ticker') or 'unknown'}"
                ),
                f"    polymarket condition id: {record.get('polymarket_condition_id')}",
                (
                    "    polymarket tokens: "
                    f"{', '.join(record.get('polymarket_token_ids') or []) or 'unknown'}"
                ),
                (
                    f"    polymarket slug: {record.get('polymarket_slug') or 'unknown'} | "
                    f"url: {record.get('polymarket_url') or 'not derivable'}"
                ),
                "  verify manually before trusting this match:",
            ]
        )
        for name in CHECKLIST_FIELDS:
            if record.get(name):
                lines.append(f"    [ ] {_CHECKLIST_LABELS[name]}")
        lines.extend(
            [
                f"  rule evidence: {record.get('rule_evidence_summary')}",
                f"  Kalshi rules excerpt: {record.get('kalshi_rules_excerpt') or 'missing'}",
                (
                    "  Polymarket rules excerpt: "
                    f"{record.get('polymarket_rules_excerpt') or 'missing'}"
                ),
                "",
            ]
        )
    return lines
