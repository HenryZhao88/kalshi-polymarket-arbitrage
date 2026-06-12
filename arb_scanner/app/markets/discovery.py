"""Cross-venue market discovery, normalization, and conservative diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from arb_scanner.app.markets.matching import similarity
from arb_scanner.app.markets.parsers import (
    US_STATE_ABBREVIATIONS,
    Evidence,
    EvidenceConfidence,
    ParsedFeatures,
    parse_features,
)
from arb_scanner.app.markets.polymarket import PolymarketMarket
from arb_scanner.app.markets.rule_equivalence import (
    KalshiRuleFacts,
    MatchStatus,
    PolymarketRuleFacts,
    RuleEquivalenceResult,
    cancellation_policy_basis,
    cancellation_policy_terms,
    decide_status,
    source_finalization_basis,
    source_finalization_terms,
    validate_rules,
)
from arb_scanner.app.markets.tickers import TickerInference, parse_kalshi_ticker


class ManualReviewSort(StrEnum):
    SIMILARITY = "similarity"
    CONFIDENCE = "confidence"
    HYPOTHETICAL_EDGE = "hypothetical_edge"
    MISSING_FIELDS = "missing_fields"
    CATEGORY = "category"
    EVENT_DATE = "event_date"
    MARKET_TYPE = "market_type"
    FEE_CONFIDENCE = "fee_confidence"


#: Diagnostic ordering only: better-attested fee sources sort first. Not a
#: pricing input — fee math still fails closed on anything but venue metadata.
_FEE_CONFIDENCE_RANK = {
    "market_metadata": 0,
    "category_default": 1,
    "unknown": 2,
}


_VOID_TERMS: tuple[tuple[str, str], ...] = (
    ("50-50", r"\b50\s*[-/]\s*50\b|\bsplit equally\b"),
    ("fair_value", r"\bfair[- ]value\b"),
    ("dnp", r"\bDNP\b|\bdid not play\b|\bdoes not play\b"),
    ("void", r"\bvoid(?:ed)?\b|\bno action\b"),
    ("refund", r"\brefund(?:ed)?\b"),
)
_SPORTS_TERMS: tuple[tuple[str, str], ...] = (
    ("postponed", r"\bpostpon(?:e|ed|ement)\b"),
    ("cancelled", r"\bcancel(?:led|ed|lation)?\b"),
    ("rescheduled", r"\breschedul(?:e|ed|ing)\b"),
    ("abandoned", r"\babandon(?:ed|ment)?\b"),
)
_DISPUTE_TERMS: tuple[tuple[str, str], ...] = (
    ("uma", r"\bUMA\b"),
    ("dispute", r"\bdisput(?:e|ed|ing)\b"),
    ("challenge", r"\bchallenge(?:d| window)?\b"),
    ("oracle", r"\boracle\b"),
)
_STATE_NAMES = US_STATE_ABBREVIATIONS


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """One raw candidate with enough provenance for review and persistence."""

    kalshi_ticker: str
    kalshi_title: str
    poly_condition_id: str
    poly_question: str
    poly_yes_token_id: str
    poly_no_token_id: str
    confidence: float
    status: MatchStatus
    matched_tokens: tuple[str, ...] = ()
    matched_fields: dict[str, Any] = field(default_factory=dict)
    differing_fields: dict[str, Any] = field(default_factory=dict)
    missing_rule_fields: tuple[str, ...] = ()
    rule_warnings: tuple[str, ...] = ()
    status_reasons: tuple[str, ...] = ()
    metadata_excerpts: dict[str, Any] = field(default_factory=dict)
    fee_confidence: str = "unknown"
    hypothetical_economics: dict[str, Any] | None = None
    scan_id: str | None = None

    def persisted_fields(self) -> dict[str, Any]:
        return {
            **self.matched_fields,
            "scan_id": self.scan_id,
            "kalshi_title": self.kalshi_title,
            "poly_question": self.poly_question,
            "matched_tokens": list(self.matched_tokens),
            "missing_rule_fields": list(self.missing_rule_fields),
            "status_reasons": list(self.status_reasons),
            "metadata_excerpts": self.metadata_excerpts,
            "fee_confidence": self.fee_confidence,
            "hypothetical_economics": self.hypothetical_economics,
        }


def _edge_value(value: dict[str, Any] | None) -> float | None:
    if not value:
        return None
    for key in ("net_edge", "net", "edge"):
        raw = value.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def diagnostic_sort_key(
    *,
    mode: ManualReviewSort,
    confidence: float,
    missing_fields: tuple[str, ...] | list[str],
    category: str | None,
    event_dates: tuple[str | None, str | None],
    hypothetical_economics: dict[str, Any] | None,
    market_type: str | None = None,
    fee_confidence: str | None = None,
    today: date | None = None,
) -> tuple[Any, ...]:
    if mode is ManualReviewSort.HYPOTHETICAL_EDGE:
        edge = _edge_value(hypothetical_economics)
        return (edge is None, -(edge or 0.0), -confidence)
    if mode is ManualReviewSort.MISSING_FIELDS:
        return (len(missing_fields), -confidence)
    if mode is ManualReviewSort.CATEGORY:
        return (category or "~unknown", -confidence)
    if mode is ManualReviewSort.MARKET_TYPE:
        return (market_type or "~unknown", -confidence)
    if mode is ManualReviewSort.FEE_CONFIDENCE:
        rank = _FEE_CONFIDENCE_RANK.get(fee_confidence or "", len(_FEE_CONFIDENCE_RANK))
        return (rank, -confidence)
    if mode is ManualReviewSort.EVENT_DATE:
        reference = today or datetime.now(UTC).date()
        parsed: list[date] = []
        for value in event_dates:
            if value:
                try:
                    parsed.append(date.fromisoformat(value))
                except ValueError:
                    continue
        distance = min((abs((value - reference).days) for value in parsed), default=10**9)
        return (distance, -confidence)
    return (-confidence,)


def sort_manual_review_pairs(pairs: list[MatchedPair], mode: ManualReviewSort) -> list[MatchedPair]:
    return sorted(
        pairs,
        key=lambda pair: diagnostic_sort_key(
            mode=mode,
            confidence=pair.confidence,
            missing_fields=pair.missing_rule_fields,
            category=str(pair.matched_fields.get("category") or "") or None,
            event_dates=(
                pair.matched_fields.get("kalshi_event_date"),
                pair.matched_fields.get("poly_event_date"),
            ),
            hypothetical_economics=pair.hypothetical_economics,
            market_type=(
                pair.matched_fields.get("kalshi_market_type")
                or pair.matched_fields.get("poly_market_type")
            ),
            fee_confidence=pair.fee_confidence,
        ),
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def kalshi_is_scannable(market: dict[str, Any]) -> bool:
    status = market.get("status")
    return (
        not market.get("mve_collection_ticker")
        and "MVE" not in str(market.get("ticker") or "")
        and status in {"active", "open"}
    )


def _policy_terms(text: str, patterns: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(name for name, pattern in patterns if re.search(pattern, text, re.IGNORECASE))


def _void_policy(payload: dict[str, Any], rules_text: str) -> str | None:
    explicit = payload.get("void_policy") or payload.get("voidPolicy")
    if explicit:
        return str(explicit).strip().lower()
    terms = _policy_terms(rules_text, _VOID_TERMS)
    return ",".join(terms) if terms else None


def _threshold(payload: dict[str, Any], title: str, reference: datetime | None) -> Decimal | None:
    parsed = parse_features(title, reference_time=reference).strike
    if parsed is not None:
        return parsed
    # Gamma's groupItemThreshold is commonly an outcome index (0, 1, 2), not
    # a strike. Explicit question text is parsed above; only venue-specific
    # line/strike fields are safe to interpret numerically here.
    keys = ["strike", "strike_value"]
    if payload.get("sportsMarketType"):
        keys.insert(0, "line")
    if payload.get("strike_type"):
        keys.extend(("floor_strike", "cap_strike"))
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            try:
                return Decimal(str(value).replace(",", ""))
            except InvalidOperation:
                continue
    return None


def _evidence(value: Evidence | None) -> dict[str, str] | None:
    return value.as_dict() if value else None


def _effective_evidence(primary: Evidence | None, fallback: Evidence | None) -> Evidence | None:
    return primary or fallback


def _state_evidence(text: str, source: str) -> Evidence | None:
    lowered = text.lower()
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return Evidence(_STATE_NAMES[name], EvidenceConfidence.HIGH, source)
    return None


def _party_evidence(text: str, source: str) -> Evidence | None:
    lowered = text.lower()
    for value, pattern in (
        ("republican", r"\b(?:republican|republicans|gop)\b"),
        ("democratic", r"\b(?:democratic|democrat|democrats)\b"),
        ("independent", r"\bindependent\b"),
    ):
        if re.search(pattern, lowered):
            return Evidence(value, EvidenceConfidence.HIGH, source)
    return None


def _confident_conflict(name: str, left: Evidence | None, right: Evidence | None) -> str | None:
    if left is None or right is None or left.value == right.value:
        return None
    if EvidenceConfidence.LOW in {left.confidence, right.confidence}:
        return None
    return f"{name} {left.value} ({left.source}) != {right.value} ({right.source})"


def _kalshi_determination_time(market: dict[str, Any]) -> datetime | None:
    return _parse_time(
        market.get("expected_expiration_time")
        or market.get("expiration_time")
        or market.get("close_time")
    )


def determination_time(
    market: dict[str, Any] | PolymarketMarket, *, polymarket: bool
) -> datetime | None:
    if isinstance(market, PolymarketMarket):
        return market.resolution_time or market.end_time
    if polymarket:
        return _parse_time(
            market.get("umaEndDate")
            or market.get("umaEndDateIso")
            or market.get("endDate")
            or market.get("endDateIso")
        )
    return _kalshi_determination_time(market)


def _poly_market(value: dict[str, Any] | PolymarketMarket) -> PolymarketMarket:
    return value if isinstance(value, PolymarketMarket) else PolymarketMarket.from_gamma(value)


def poly_token_ids(
    market: dict[str, Any] | PolymarketMarket,
) -> tuple[str, str] | None:
    normalized = _poly_market(market)
    if len(normalized.token_ids) < 2:
        return None
    return normalized.token_ids[0], normalized.token_ids[1]


def _kalshi_excerpt(
    market: dict[str, Any],
    features: ParsedFeatures,
    ticker: TickerInference,
) -> dict[str, Any]:
    title = str(market.get("title") or "")
    determination = _kalshi_determination_time(market)
    rules_text = "\n".join(
        str(value)
        for value in (market.get("rules_primary"), market.get("rules_secondary"))
        if value
    )
    close_time = _parse_time(market.get("close_time"))
    void_terms = _policy_terms(rules_text, _VOID_TERMS)
    sports_terms = _policy_terms(rules_text, _SPORTS_TERMS)
    return {
        "ticker": str(market.get("ticker") or ""),
        "event_ticker": str(market.get("event_ticker") or "") or None,
        "title": title,
        "event_date": features.event_date.isoformat() if features.event_date else None,
        "event_year": features.event_year,
        "event_date_evidence": _evidence(features.event_date_evidence),
        "event_year_evidence": _evidence(features.event_year_evidence),
        "close_time": close_time.isoformat() if close_time else None,
        "determination_time": determination.isoformat() if determination else None,
        "threshold": (
            str(value) if (value := _threshold(market, title, determination)) is not None else None
        ),
        "resolution_source": str(market.get("resolution_source") or ""),
        "rules_text": rules_text[:1000],
        "void_policy": _void_policy(market, rules_text),
        "dnp_policy": "dnp" if "dnp" in void_terms else None,
        "fair_value_policy": "fair_value" if "fair_value" in void_terms else None,
        "fifty_fifty_policy": "50-50" if "50-50" in void_terms else None,
        "cancellation_policy_terms": list(cancellation_policy_terms(rules_text)),
        "cancellation_policy_basis": cancellation_policy_basis(rules_text),
        "source_finalization_terms": list(source_finalization_terms(f"{title}\n{rules_text}")),
        "source_finalization_basis": source_finalization_basis(f"{title}\n{rules_text}"),
        "sports_policy_terms": list(sports_terms),
        "dispute_terms": list(_policy_terms(rules_text, _DISPUTE_TERMS)),
        "market_type_evidence": _evidence(features.market_type_evidence),
        "threshold_evidence": _evidence(features.strike_evidence),
        "ticker_inference": ticker.as_dict(),
    }


def _rule_facts(
    kalshi_market: dict[str, Any],
    poly: PolymarketMarket,
    kalshi_features: ParsedFeatures,
    poly_features: ParsedFeatures,
    ticker: TickerInference,
) -> tuple[KalshiRuleFacts, PolymarketRuleFacts, dict[str, Any], dict[str, Any]]:
    kalshi_excerpt = _kalshi_excerpt(kalshi_market, kalshi_features, ticker)
    poly_excerpt = poly.metadata_excerpt()
    poly_void = _void_policy(poly.raw, poly.description)
    poly_void_terms = _policy_terms(poly.description, _VOID_TERMS)
    poly_sports_terms = _policy_terms(poly.description, _SPORTS_TERMS)
    poly_excerpt.update(
        {
            "event_date": (
                poly_features.event_date.isoformat() if poly_features.event_date else None
            ),
            "event_year": poly_features.event_year,
            "event_date_evidence": _evidence(poly_features.event_date_evidence),
            "event_year_evidence": _evidence(poly_features.event_year_evidence),
            "threshold": (
                str(value)
                if (value := _threshold(poly.raw, poly.question, poly.end_time)) is not None
                else None
            ),
            "void_policy": poly_void,
            "dnp_policy": "dnp" if "dnp" in poly_void_terms else None,
            "fair_value_policy": ("fair_value" if "fair_value" in poly_void_terms else None),
            "fifty_fifty_policy": "50-50" if "50-50" in poly_void_terms else None,
            "cancellation_policy_terms": list(cancellation_policy_terms(poly.description)),
            "cancellation_policy_basis": cancellation_policy_basis(poly.description),
            "source_finalization_terms": list(
                source_finalization_terms(f"{poly.question}\n{poly.description}")
            ),
            "source_finalization_basis": source_finalization_basis(
                f"{poly.question}\n{poly.description}"
            ),
            "sports_policy_terms": list(poly_sports_terms),
            "dispute_terms": list(_policy_terms(poly.description, _DISPUTE_TERMS)),
            "market_type_evidence": _evidence(poly_features.market_type_evidence),
            "threshold_evidence": _evidence(poly_features.strike_evidence),
        }
    )
    return (
        KalshiRuleFacts(
            determination_time=_kalshi_determination_time(kalshi_market),
            resolution_source=str(kalshi_market.get("resolution_source") or "")[:200],
            resolution_text=str(kalshi_excerpt["rules_text"]),
            can_close_early=bool(kalshi_market.get("can_close_early")),
            is_sports=str(kalshi_market.get("category", "")).lower() == "sports",
            void_policy=(
                str(kalshi_excerpt["void_policy"])
                if kalshi_excerpt["void_policy"] is not None
                else None
            ),
            sports_policy=tuple(str(item) for item in kalshi_excerpt["sports_policy_terms"]),
            title=str(kalshi_market.get("title") or ""),
        ),
        PolymarketRuleFacts(
            determination_time=poly.resolution_time or poly.end_time,
            resolution_source=poly.resolution_source[:200],
            resolution_text=poly.description,
            uma_resolution=True,
            is_sports=poly.category == "sports",
            game_start_time=_parse_time(
                poly.raw.get("gameStartTime") or poly.raw.get("eventStartTime")
            ),
            void_policy=poly_void,
            sports_policy=poly_sports_terms,
            title=poly.question,
        ),
        kalshi_excerpt,
        poly_excerpt,
    )


def _presence_warning(name: str, left: object | None, right: object | None) -> tuple[str, ...]:
    if (left is None) == (right is None):
        return ()
    return (f"{name} missing on one venue",)


def evaluate_pair(
    kalshi_market: dict[str, Any],
    poly_market: dict[str, Any] | PolymarketMarket,
    *,
    min_rule_review_score: float = 0.0,
) -> MatchedPair | None:
    """Evaluate one token-prefilter candidate without fetching unsafe-pair books."""
    poly = _poly_market(poly_market)
    tokens = poly_token_ids(poly)
    if tokens is None:
        return None

    kalshi_ticker = str(kalshi_market.get("ticker") or "")
    kalshi_title = str(kalshi_market.get("title") or "")
    kalshi_time = _kalshi_determination_time(kalshi_market)
    kalshi_rules = "\n".join(
        str(value)
        for value in (kalshi_market.get("rules_primary"), kalshi_market.get("rules_secondary"))
        if value
    )
    kalshi_category = str(kalshi_market.get("category") or "") or None
    kalshi_features = parse_features(
        kalshi_title,
        reference_time=kalshi_time,
        description=kalshi_rules,
        category=kalshi_category,
    )
    poly_features = parse_features(
        poly.question,
        reference_time=poly.end_time,
        description=poly.description,
        category=poly.category,
    )
    ticker = parse_kalshi_ticker(kalshi_ticker)
    sim = similarity(
        kalshi_title,
        poly.question,
        determination_time_a=kalshi_time,
        determination_time_b=poly.resolution_time or poly.end_time,
    )
    if sim.score < min_rule_review_score:
        return MatchedPair(
            kalshi_ticker=kalshi_ticker,
            kalshi_title=kalshi_title,
            poly_condition_id=poly.condition_id,
            poly_question=poly.question,
            poly_yes_token_id=tokens[0],
            poly_no_token_id=tokens[1],
            confidence=round(sim.score, 4),
            status=MatchStatus.REJECTED,
            matched_tokens=sim.matched_tokens,
            matched_fields={
                "similarity_stage": sim.stage.value,
                "kalshi_market_type": (
                    kalshi_features.market_type.value if kalshi_features.market_type else None
                ),
                "poly_market_type": (
                    poly_features.market_type.value if poly_features.market_type else None
                ),
            },
            differing_fields={
                f"conflict_{index}": value for index, value in enumerate(sim.structured_conflicts)
            },
            status_reasons=(
                *sim.structured_conflicts,
                "similarity below structured-review threshold",
            ),
            metadata_excerpts={
                "kalshi": {
                    "ticker": kalshi_ticker,
                    "title": kalshi_title,
                    "close_time": str(kalshi_market.get("close_time") or "") or None,
                    "determination_time": kalshi_time.isoformat() if kalshi_time else None,
                    "ticker_inference": ticker.as_dict(),
                },
                "polymarket": poly.metadata_excerpt(),
            },
            fee_confidence=poly.fee_confidence,
        )
    kalshi_threshold = _threshold(kalshi_market, kalshi_title, kalshi_time)
    poly_threshold = _threshold(poly.raw, poly.question, poly.end_time)
    kalshi_threshold_evidence = kalshi_features.strike_evidence
    if kalshi_threshold_evidence is None and kalshi_threshold is not None:
        kalshi_threshold_evidence = Evidence(
            str(kalshi_threshold), EvidenceConfidence.HIGH, "metadata"
        )
    poly_threshold_evidence = poly_features.strike_evidence
    if poly_threshold_evidence is None and poly_threshold is not None:
        poly_threshold_evidence = Evidence(str(poly_threshold), EvidenceConfidence.HIGH, "metadata")

    kalshi_type_evidence = _effective_evidence(
        kalshi_features.market_type_evidence, ticker.market_type
    )
    poly_type_evidence = poly_features.market_type_evidence
    kalshi_state_evidence = _effective_evidence(
        _state_evidence(kalshi_title, "title"), ticker.state
    )
    poly_state_evidence = _state_evidence(poly.question, "title")
    kalshi_party_evidence = _party_evidence(kalshi_title, "title")
    poly_party_evidence = _party_evidence(poly.question, "title")
    kalshi_date_evidence = _effective_evidence(
        kalshi_features.event_date_evidence, ticker.event_date
    )
    poly_date_evidence = poly_features.event_date_evidence
    kalshi_year_evidence = _effective_evidence(
        kalshi_features.event_year_evidence, ticker.event_year
    )
    poly_year_evidence = poly_features.event_year_evidence
    effective_k_threshold = _effective_evidence(kalshi_threshold_evidence, ticker.threshold)

    kalshi_facts, poly_facts, kalshi_excerpt, poly_excerpt = _rule_facts(
        kalshi_market, poly, kalshi_features, poly_features, ticker
    )
    rules: RuleEquivalenceResult = validate_rules(kalshi_facts, poly_facts)
    presence_warnings: tuple[str, ...] = (
        *_presence_warning("market_type", kalshi_type_evidence, poly_type_evidence),
        *_presence_warning("event_date", kalshi_date_evidence, poly_date_evidence),
        *_presence_warning("event_year", kalshi_year_evidence, poly_year_evidence),
        *_presence_warning("threshold", effective_k_threshold, poly_threshold_evidence),
    )
    ticker_only_warnings = tuple(
        f"{name} inferred from Kalshi ticker only"
        for name, primary, effective in (
            ("market_type", kalshi_features.market_type_evidence, kalshi_type_evidence),
            ("event_date", kalshi_features.event_date_evidence, kalshi_date_evidence),
            ("threshold", kalshi_threshold_evidence, effective_k_threshold),
        )
        if primary is None and effective is not None and effective.source == "ticker"
    )
    structured_conflicts = list(sim.structured_conflicts)
    for name, left, right in (
        ("market type", kalshi_type_evidence, poly_type_evidence),
        ("event date", kalshi_date_evidence, poly_date_evidence),
        ("event year", kalshi_year_evidence, poly_year_evidence),
        ("threshold", effective_k_threshold, poly_threshold_evidence),
        ("state", kalshi_state_evidence, poly_state_evidence),
        ("outcome party", kalshi_party_evidence, poly_party_evidence),
    ):
        conflict = _confident_conflict(name, left, right)
        if conflict and not any(conflict in existing for existing in structured_conflicts):
            structured_conflicts.append(conflict)

    combined_rules = RuleEquivalenceResult(
        hard_failures=rules.hard_failures,
        warnings=(*rules.warnings, *presence_warnings, *ticker_only_warnings),
        missing_fields=(
            *rules.missing_fields,
            *(warning.split()[0] for warning in presence_warnings),
            *(warning.split()[0] for warning in ticker_only_warnings),
        ),
    )
    status = decide_status(sim.score, combined_rules)
    if structured_conflicts:
        status = MatchStatus.REJECTED

    differing: dict[str, Any] = {
        **{f"conflict_{index}": value for index, value in enumerate(structured_conflicts)},
        **{f"rule_{index}": value for index, value in enumerate(rules.hard_failures)},
    }
    kalshi_unmatched = sorted(kalshi_features.tokens - set(sim.matched_tokens))
    poly_unmatched = sorted(poly_features.tokens - set(sim.matched_tokens))
    if kalshi_unmatched:
        differing["kalshi_unmatched_title_tokens"] = kalshi_unmatched
    if poly_unmatched:
        differing["poly_unmatched_title_tokens"] = poly_unmatched
    if sim.score < 0.6:
        status_reasons = (
            *structured_conflicts,
            *rules.hard_failures,
            "similarity below review threshold",
        )
    elif status is MatchStatus.REJECTED:
        status_reasons = (*structured_conflicts, *rules.hard_failures)
    elif status is MatchStatus.MANUAL_REVIEW:
        status_reasons = (*combined_rules.warnings, "not enough verified evidence to accept")
    else:
        status_reasons = ("verified structured metadata and rules support equivalence",)

    return MatchedPair(
        kalshi_ticker=kalshi_ticker,
        kalshi_title=kalshi_title,
        poly_condition_id=poly.condition_id,
        poly_question=poly.question,
        poly_yes_token_id=tokens[0],
        poly_no_token_id=tokens[1],
        confidence=round(sim.score, 4),
        status=status,
        matched_tokens=sim.matched_tokens,
        matched_fields={
            "similarity_stage": sim.stage.value,
            "kalshi_event_date": kalshi_date_evidence.value if kalshi_date_evidence else None,
            "poly_event_date": poly_date_evidence.value if poly_date_evidence else None,
            "kalshi_event_year": (
                int(kalshi_year_evidence.value) if kalshi_year_evidence else None
            ),
            "poly_event_year": poly_features.event_year,
            "kalshi_threshold": (effective_k_threshold.value if effective_k_threshold else None),
            "poly_threshold": (poly_threshold_evidence.value if poly_threshold_evidence else None),
            "kalshi_market_type": (kalshi_type_evidence.value if kalshi_type_evidence else None),
            "poly_market_type": poly_type_evidence.value if poly_type_evidence else None,
            "kalshi_market_type_evidence": _evidence(kalshi_type_evidence),
            "poly_market_type_evidence": _evidence(poly_type_evidence),
            "kalshi_event_date_evidence": _evidence(kalshi_date_evidence),
            "poly_event_date_evidence": _evidence(poly_date_evidence),
            "kalshi_threshold_evidence": _evidence(effective_k_threshold),
            "poly_threshold_evidence": _evidence(poly_threshold_evidence),
            "kalshi_state": kalshi_state_evidence.value if kalshi_state_evidence else None,
            "poly_state": poly_state_evidence.value if poly_state_evidence else None,
            "kalshi_state_evidence": _evidence(kalshi_state_evidence),
            "poly_state_evidence": _evidence(poly_state_evidence),
            "kalshi_outcome_party": (
                kalshi_party_evidence.value if kalshi_party_evidence else None
            ),
            "poly_outcome_party": poly_party_evidence.value if poly_party_evidence else None,
            "category": poly.category or kalshi_category,
        },
        differing_fields=differing,
        missing_rule_fields=tuple(dict.fromkeys(combined_rules.missing_fields)),
        rule_warnings=combined_rules.warnings,
        status_reasons=tuple(dict.fromkeys(reason for reason in status_reasons if reason)),
        metadata_excerpts={"kalshi": kalshi_excerpt, "polymarket": poly_excerpt},
        fee_confidence=poly.fee_confidence,
    )
