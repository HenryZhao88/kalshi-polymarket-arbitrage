"""Export, checklist, and verification-packet rendering tests.

Builds MatchedPairRow objects directly (no DB) — the export layer must
tolerate both freshly persisted rows and rows written by older scanner
versions that lack newer identifiers.
"""

import csv
import io
import json
from typing import Any

from arb_scanner.app.storage.export import (
    CHECKLIST_FIELDS,
    EXPORT_FIELDS,
    NOT_TRADE_SAFE_LABEL,
    pair_record,
    polymarket_public_url,
    render_csv,
    render_json,
    render_verification_packet,
    verification_checklist,
)
from arb_scanner.app.storage.models import MatchedPairRow


def make_row(**overrides: Any) -> MatchedPairRow:
    matched_fields: dict[str, Any] = {
        "scan_id": "scan-1",
        "kalshi_title": "Will the Republican party win the governorship in South Carolina",
        "poly_question": "Will the Republicans win the South Carolina governor race in 2026?",
        "matched_tokens": ["carolina", "south", "win"],
        "missing_rule_fields": ["determination_time", "resolution_source", "void_policy"],
        "status_reasons": [
            "determination time unverified on at least one venue",
            "not enough verified evidence to accept",
        ],
        "kalshi_market_type": "governor_winner",
        "poly_market_type": "governor_winner",
        "kalshi_event_date": None,
        "poly_event_date": None,
        "kalshi_threshold": None,
        "poly_threshold": None,
        "category": "politics",
        "fee_confidence": "market_metadata",
        "hypothetical_economics": None,
        "metadata_excerpts": {
            "kalshi": {
                "ticker": "GOVPARTYSC-26-R",
                "event_ticker": "GOVPARTYSC-26",
                "close_time": "2027-11-03T15:00:00+00:00",
                "determination_time": "2027-01-24T15:00:00+00:00",
                "resolution_source": "",
                "void_policy": None,
                "rules_text": "If a Republican is inaugurated, resolves Yes.",
                "sports_policy_terms": [],
            },
            "polymarket": {
                "slug": "republicans-sc-governor-2026",
                "event_slug": "south-carolina-governor-2026",
                "end_time": None,
                "resolution_time": None,
                "resolution_source": "",
                "void_policy": None,
                "description": "Resolves to the winner of the 2026 SC gubernatorial election.",
                "dispute_terms": ["uma"],
            },
        },
    }
    matched_fields.update(overrides.pop("matched_fields", {}))
    defaults: dict[str, Any] = {
        "kalshi_ticker": "GOVPARTYSC-26-R",
        "poly_condition_id": "0x1b7fa10e",
        "poly_yes_token_id": "111",
        "poly_no_token_id": "222",
        "confidence": 0.8247,
        "status": "manual_review",
        "matched_fields": matched_fields,
        "differing_fields": {
            "kalshi_unmatched_title_tokens": ["governorship", "party", "republican"],
        },
        "rule_warnings": [],
    }
    defaults.update(overrides)
    return MatchedPairRow(**defaults)


VERIFIED_FIELDS: dict[str, Any] = {
    "missing_rule_fields": [],
    "kalshi_event_date": "2026-11-03",
    "poly_event_date": "2026-11-03",
    "kalshi_threshold": "70000",
    "poly_threshold": "70000",
    "hypothetical_economics": {"net_edge": 0.01},
    "metadata_excerpts": {
        "kalshi": {
            "event_ticker": "EV",
            "close_time": "2026-11-03T15:00:00+00:00",
            "determination_time": "2026-11-10T15:00:00+00:00",
            "resolution_source": "official canvass",
            "void_policy": "none",
            "rules_text": "rules",
            "sports_policy_terms": [],
        },
        "polymarket": {
            "slug": "s",
            "event_slug": "e",
            "end_time": "2026-11-03T15:00:00+00:00",
            "resolution_time": "2026-11-12T15:00:00+00:00",
            "resolution_source": "official canvass",
            "void_policy": "none",
            "description": "rules",
            "dispute_terms": [],
        },
    },
}


class TestPairRecord:
    def test_core_fields_and_label(self) -> None:
        record = pair_record(make_row())
        assert record["status"] == "manual_review"
        assert record["confidence"] == 0.8247
        assert record["not_trade_safe_label"] == NOT_TRADE_SAFE_LABEL
        assert "not enough verified evidence to accept" in record["reason"]
        assert record["hypothetical_edge_status"] == "not_computed"
        assert record["unsafe_hypothetical_edge_if_available"] is None

    def test_venue_identifiers(self) -> None:
        record = pair_record(make_row())
        assert record["kalshi_ticker"] == "GOVPARTYSC-26-R"
        assert record["kalshi_event_ticker"] == "GOVPARTYSC-26"
        assert record["polymarket_condition_id"] == "0x1b7fa10e"
        assert record["polymarket_slug"] == "republicans-sc-governor-2026"
        assert record["polymarket_token_ids"] == ["111", "222"]
        assert (
            record["polymarket_url"]
            == "https://polymarket.com/event/south-carolina-governor-2026"
        )
        # No public Kalshi URL is derivable from a ticker; identifiers only.
        assert "kalshi_url" not in record

    def test_tolerates_rows_persisted_before_identifier_enrichment(self) -> None:
        record = pair_record(make_row(matched_fields={"metadata_excerpts": {}}))
        assert record["kalshi_event_ticker"] is None
        assert record["polymarket_slug"] is None
        assert record["polymarket_url"] is None
        assert record["kalshi_void_policy"] is None

    def test_blocking_summary_fields(self) -> None:
        record = pair_record(make_row())
        # No hard conflicts and no *_mismatch reasons -> first missing field.
        assert record["primary_blocker"] == "determination_time"
        assert record["diagnostic_reasons"] == []
        assert record["unresolved_fields"] == record["missing_fields"]
        assert record["next_human_action"] == (
            "verify determination/settlement timing on both venues"
        )
        assert record["evidence_confidence_summary"] == (
            "type=none/none date=none/none threshold=none/none entity=none/none "
            "fee=market_metadata"
        )
        with_evidence = make_row(
            matched_fields={
                "kalshi_market_type_evidence": {"value": "x", "confidence": "high"},
                "poly_market_type_evidence": {"value": "x", "confidence": "medium"},
            }
        )
        assert pair_record(with_evidence)["evidence_confidence_summary"].startswith(
            "type=high/medium"
        )

    def test_blocking_summary_prefers_diagnostic_mismatch(self) -> None:
        row = make_row(
            matched_fields={
                "status_reasons": [
                    "source_finalization_mismatch: kalshi=fixed_time_snapshot "
                    "polymarket=official_close — underlying value is finalized "
                    "differently on each venue",
                    "not enough verified evidence to accept",
                ],
            }
        )
        record = pair_record(row)
        assert record["primary_blocker"] == "source_finalization_mismatch"
        assert len(record["diagnostic_reasons"]) == 1
        assert record["next_human_action"] == (
            "compare venue source/finalization rules and official close policy"
        )

    def test_blocking_summary_prefers_hard_conflict_over_everything(self) -> None:
        row = make_row(
            differing_fields={"rule_0": "candidate_set_conflict: different slates"}
        )
        record = pair_record(row)
        assert record["primary_blocker"] == "candidate_set_conflict"
        assert record["next_human_action"] == (
            "none — pair is rejected by a structured conflict"
        )

    def test_source_finalization_basis_fields(self) -> None:
        row = make_row()
        row.matched_fields["metadata_excerpts"]["kalshi"][
            "source_finalization_basis"
        ] = "fixed_time_snapshot"
        record = pair_record(row)
        assert record["kalshi_source_finalization_basis"] == "fixed_time_snapshot"
        assert record["polymarket_source_finalization_basis"] is None
        assert "finalization=fixed_time_snapshot" in record["rule_evidence_summary"]

    def test_stat_tie_policy_fields(self) -> None:
        row = make_row()
        row.matched_fields["metadata_excerpts"]["kalshi"]["stat_tie_policy"] = "ties_split"
        row.matched_fields["metadata_excerpts"]["polymarket"][
            "stat_tie_policy"
        ] = "sole_winner_tiebreak"
        record = pair_record(row)
        assert record["kalshi_stat_tie_policy"] == "ties_split"
        assert record["polymarket_stat_tie_policy"] == "sole_winner_tiebreak"
        # Old rows export None, not a guess.
        old = pair_record(make_row(matched_fields={"metadata_excerpts": {}}))
        assert old["kalshi_stat_tie_policy"] is None

    def test_stat_leader_next_action_mapping(self) -> None:
        row = make_row(
            matched_fields={
                "status_reasons": [
                    "stat_leader_rule_mismatch: tie policy kalshi=ties_split "
                    "polymarket=sole_winner_tiebreak — exact ties may pay "
                    "differently on each venue",
                ],
            }
        )
        record = pair_record(row)
        assert record["primary_blocker"] == "stat_leader_rule_mismatch"
        assert record["next_human_action"] == (
            "verify official stats source, tie handling, and regular-season scope"
        )

    def test_cancellation_policy_basis_fields(self) -> None:
        row = make_row()
        row.matched_fields["metadata_excerpts"]["polymarket"][
            "cancellation_policy_basis"
        ] = "resolves_to_other"
        record = pair_record(row)
        assert record["polymarket_cancellation_policy_basis"] == "resolves_to_other"
        assert record["kalshi_cancellation_policy_basis"] is None  # not extracted
        assert "cancellation=resolves_to_other" in record["rule_evidence_summary"]
        assert "cancellation=unknown" in record["rule_evidence_summary"]
        # Rows persisted before this field existed export None, not a guess.
        old = pair_record(make_row(matched_fields={"metadata_excerpts": {}}))
        assert old["polymarket_cancellation_policy_basis"] is None

    def test_comparison_fields(self) -> None:
        row = make_row(
            differing_fields={
                "conflict_0": "threshold 70000 (title) != 80000 (title)",
                "kalshi_unmatched_title_tokens": ["party"],
            }
        )
        record = pair_record(row)
        assert record["missing_fields"] == [
            "determination_time",
            "resolution_source",
            "void_policy",
        ]
        assert record["conflicting_fields"] == ["threshold 70000 (title) != 80000 (title)"]
        assert record["mismatched_fields"] == {"kalshi_unmatched_title_tokens": ["party"]}
        assert record["matched_tokens"] == ["carolina", "south", "win"]
        assert "kalshi" in record["rule_evidence_summary"]
        assert "polymarket" in record["rule_evidence_summary"]
        assert "inaugurated" in record["kalshi_rules_excerpt"]

    def test_no_secret_like_keys_in_record(self) -> None:
        record = pair_record(make_row())
        suspicious = ("key", "secret", "password", "token_secret", "passphrase", "webhook")
        for name in record:
            assert not any(term in name for term in suspicious if name != "polymarket_token_ids")


class TestPolymarketUrl:
    def test_derived_from_event_slug(self) -> None:
        assert polymarket_public_url("abc-2026") == "https://polymarket.com/event/abc-2026"

    def test_missing_slug_yields_no_url(self) -> None:
        assert polymarket_public_url(None) is None
        assert polymarket_public_url("") is None


class TestVerificationChecklist:
    def test_unverified_governor_row_flags_critical_fields(self) -> None:
        record = pair_record(make_row())
        assert record["needs_determination_time"] is True
        assert record["needs_resolution_source"] is True
        assert record["needs_void_policy"] is True
        # Both venues lack a date/threshold entirely — still needs human eyes.
        assert record["needs_event_date_confirmation"] is True
        assert record["needs_threshold_confirmation"] is True
        # Both typed governor_winner with title evidence → no type flag.
        assert record["needs_market_type_confirmation"] is False
        assert record["needs_fee_confirmation"] is False
        assert record["needs_liquidity_confirmation"] is True

    def test_fully_attested_row_clears_checklist(self) -> None:
        record = pair_record(make_row(matched_fields=dict(VERIFIED_FIELDS)))
        for name in CHECKLIST_FIELDS:
            assert record[name] is False, name

    def test_conflicting_threshold_flags_confirmation(self) -> None:
        fields = dict(VERIFIED_FIELDS)
        fields["poly_threshold"] = "80000"
        record = pair_record(make_row(matched_fields=fields))
        assert record["needs_threshold_confirmation"] is True

    def test_conflict_strings_flag_named_fields(self) -> None:
        fields = dict(VERIFIED_FIELDS)
        row = make_row(
            matched_fields=fields,
            differing_fields={"conflict_0": "market type a != b"},
        )
        record = pair_record(row)
        assert record["needs_market_type_confirmation"] is True

    def test_non_metadata_fee_confidence_flags_fees(self) -> None:
        for confidence in ("unknown", "category_default"):
            fields = dict(VERIFIED_FIELDS)
            fields["fee_confidence"] = confidence
            record = pair_record(make_row(matched_fields=fields))
            assert record["needs_fee_confirmation"] is True

    def test_checklist_is_diagnostic_not_acceptance(self) -> None:
        # Clearing every checklist item must not change the persisted status.
        record = pair_record(make_row(matched_fields=dict(VERIFIED_FIELDS)))
        assert record["status"] == "manual_review"
        assert record["not_trade_safe_label"] == NOT_TRADE_SAFE_LABEL

    def test_checklist_keys_and_order_are_stable(self) -> None:
        checklist = verification_checklist({})
        assert tuple(checklist) == CHECKLIST_FIELDS


class TestCsvRendering:
    def test_headers_are_stable_and_first(self) -> None:
        rendered = render_csv([pair_record(make_row())])
        reader = csv.reader(io.StringIO(rendered))
        assert next(reader) == list(EXPORT_FIELDS)

    def test_rows_round_trip_and_preserve_order(self) -> None:
        records = [
            pair_record(make_row(kalshi_ticker="A-1", confidence=0.9)),
            pair_record(make_row(kalshi_ticker="B-2", confidence=0.5)),
        ]
        reader = csv.DictReader(io.StringIO(render_csv(records)))
        rows = list(reader)
        assert [row["kalshi_ticker"] for row in rows] == ["A-1", "B-2"]
        assert rows[0]["not_trade_safe_label"] == NOT_TRADE_SAFE_LABEL
        assert rows[0]["needs_void_policy"] == "true"
        assert rows[0]["polymarket_token_ids"] == "111; 222"
        assert rows[0]["kalshi_event_date"] == ""  # None renders empty, not "None"

    def test_empty_record_list_still_has_headers(self) -> None:
        reader = csv.reader(io.StringIO(render_csv([])))
        assert next(reader) == list(EXPORT_FIELDS)


class TestJsonRendering:
    def test_valid_structured_json(self) -> None:
        rendered = render_json(
            [pair_record(make_row())], mode="manual_review", sort="missing_fields"
        )
        payload = json.loads(rendered)
        assert payload["label"] == NOT_TRADE_SAFE_LABEL
        assert payload["mode"] == "manual_review"
        assert payload["sort"] == "missing_fields"
        assert payload["row_count"] == 1
        row = payload["rows"][0]
        # Structured values, not stringified text blocks.
        assert row["missing_fields"] == [
            "determination_time",
            "resolution_source",
            "void_policy",
        ]
        assert row["polymarket_token_ids"] == ["111", "222"]
        assert row["needs_void_policy"] is True

    def test_disclaimer_present(self) -> None:
        payload = json.loads(render_json([], mode="manual_review", sort="similarity"))
        assert "No row is a trade recommendation" in payload["disclaimer"]


class TestVerificationPacket:
    def test_every_row_labeled_not_trade_safe(self) -> None:
        records = [pair_record(make_row(kalshi_ticker=f"T-{i}")) for i in range(3)]
        lines = render_verification_packet(records)
        text = "\n".join(lines)
        assert text.count(NOT_TRADE_SAFE_LABEL) >= len(records) + 1  # header + rows
        for i in range(3):
            assert f"T-{i}" in text

    def test_packet_contains_blocking_summary(self) -> None:
        text = "\n".join(render_verification_packet([pair_record(make_row())]))
        assert "blocking summary:" in text
        assert "primary blocker: determination_time" in text
        assert "next action: verify determination/settlement timing" in text

    def test_packet_contains_checklist_and_identifiers(self) -> None:
        text = "\n".join(render_verification_packet([pair_record(make_row())]))
        assert "no claim of arbitrage or profitability" in text
        assert "why it matched" in text
        assert "why not accepted" in text
        assert "[ ] determination time verified on both venues" in text
        assert "unresolved fields blocking acceptance: determination_time" in text
        assert "polymarket condition id: 0x1b7fa10e" in text
        assert "https://polymarket.com/event/south-carolina-governor-2026" in text
        assert "rules excerpt" in text

    def test_cleared_checklist_rows_render_without_boxes(self) -> None:
        record = pair_record(make_row(matched_fields=dict(VERIFIED_FIELDS)))
        text = "\n".join(render_verification_packet([record]))
        assert "[ ]" not in text
        assert NOT_TRADE_SAFE_LABEL in text
