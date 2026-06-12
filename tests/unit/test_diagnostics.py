"""Live-regression log gate: parsing, expectations, and exit codes."""

from pathlib import Path

import pytest

from arb_scanner.app.diagnostics import (
    Expectation,
    evaluate_expectations,
    parse_funnel,
    parse_histogram,
    run_check_log,
)

SAMPLE_LOG = """\
2026-06-11 12:00:00 arb_scanner discovery pages=50 fetched=5000
markets: Kalshi discovered=65416 scannable=65414, Polymarket discovered=5000 scannable=4945
candidate funnel: raw_title=38938 structured=1330 manual_review=9 accepted=0 rejected=38929
rejections by reason: determination_time_conflict=37526, market_type_conflict=969, \
continent_scope_conflict=1, basket_scope_conflict=2
manual review (top 9; NOT TRADE SAFE):
[1] NOT TRADE SAFE | similarity=0.79
  reasons: source_finalization_mismatch: kalshi=fixed_time_snapshot polymarket=official_close
[2] NOT TRADE SAFE | similarity=0.94
  reasons: void_policy_mismatch: kalshi=unknown polymarket=resolves_to_other
"""


class TestParsing:
    def test_parse_funnel(self) -> None:
        funnel = parse_funnel(SAMPLE_LOG)
        assert funnel["accepted"] == 0
        assert funnel["manual_review"] == 9
        assert funnel["rejected"] == 38929

    def test_parse_histogram(self) -> None:
        histogram = parse_histogram(SAMPLE_LOG)
        assert histogram["continent_scope_conflict"] == 1
        assert histogram["market_type_conflict"] == 969

    def test_last_occurrence_wins(self) -> None:
        doubled = SAMPLE_LOG + "\ncandidate funnel: raw_title=1 structured=1 accepted=1\n"
        assert parse_funnel(doubled)["accepted"] == 1

    def test_missing_lines_parse_empty(self) -> None:
        assert parse_funnel("no funnel here") == {}
        assert parse_histogram("no histogram here") == {}


class TestExpectations:
    def test_parse_operators(self) -> None:
        for text, op, value in (
            ("accepted=0", "=", 0),
            ("manual_review<=10", "<=", 10),
            ("continent_scope_conflict>=1", ">=", 1),
            ("rejected>100", ">", 100),
        ):
            expectation = Expectation.parse(text)
            assert (expectation.operator, expectation.value) == (op, value)

    def test_unparseable_expectation_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable"):
            Expectation.parse("accepted is zero")

    def test_funnel_and_histogram_lookup(self) -> None:
        results = evaluate_expectations(
            SAMPLE_LOG,
            [Expectation.parse("accepted=0"), Expectation.parse("continent_scope_conflict>=1")],
        )
        assert all(result.passed for result in results)
        assert [result.source for result in results] == ["funnel", "histogram"]

    def test_absent_conflict_bucket_counts_as_zero(self) -> None:
        (result,) = evaluate_expectations(
            SAMPLE_LOG, [Expectation.parse("office_level_conflict>=1")]
        )
        assert result.source == "histogram"
        assert result.actual == 0
        assert not result.passed

    def test_diagnostic_warnings_counted_by_occurrence(self) -> None:
        (result,) = evaluate_expectations(
            SAMPLE_LOG, [Expectation.parse("source_finalization_mismatch>=1")]
        )
        assert result.source == "occurrences"
        assert result.actual == 1
        assert result.passed

    def test_dead_detector_is_caught(self) -> None:
        # The failure mode this gate exists for: a detector that tests green
        # but never fires live.
        (result,) = evaluate_expectations(
            SAMPLE_LOG, [Expectation.parse("candidate_set_conflict>=1")]
        )
        assert not result.passed


class TestRunCheckLog:
    def test_all_pass_returns_zero(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text(SAMPLE_LOG)
        assert run_check_log(str(log), ["accepted=0", "manual_review<=10"]) == 0

    def test_failed_expectation_returns_one(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text(SAMPLE_LOG)
        assert run_check_log(str(log), ["accepted=0", "candidate_set_conflict>=1"]) == 1

    def test_missing_file_returns_two(self) -> None:
        assert run_check_log("/nonexistent/path.log", ["accepted=0"]) == 2

    def test_no_expectations_returns_two(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text(SAMPLE_LOG)
        assert run_check_log(str(log), []) == 2

    def test_bad_expectation_returns_two(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text(SAMPLE_LOG)
        assert run_check_log(str(log), ["nonsense expression"]) == 2
