"""Live-regression gate for saved dry-run logs.

A detector with green unit tests can still be dead in production — the
2026-06-11 audit found `continent_scope_conflict` passing every test while
the live histogram showed it never fired. This module asserts expectations
against a SAVED dry-run log (or text report), so a live run becomes a
regression artifact:

    uv run arb-scanner check-log --file /tmp/dryrun.log \\
        --expect accepted=0 --expect "continent_scope_conflict>=1"

It only reads a local file: it never calls venue APIs and has no execution
capability of any kind.

Metric lookup order for an expectation name:
1. candidate-funnel counters (raw_title, structured, manual_review,
   accepted, rejected, pairs from the "candidate funnel:" line),
2. rejection-histogram buckets (the "rejections by reason:" line; buckets
   absent from the line count as 0),
3. otherwise, the number of occurrences of the name anywhere in the log —
   which is how diagnostic warnings like source_finalization_mismatch are
   counted, since they never appear in the rejection histogram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FUNNEL_RE = re.compile(
    r"candidate funnel:\s*(?P<body>(?:\w+=\d+\s*)+)",
)
_HISTOGRAM_RE = re.compile(r"rejections by reason:\s*(?P<body>.+)")
_PAIR_RE = re.compile(r"(\w+)=(\d+)")
_EXPECTATION_RE = re.compile(r"^\s*([\w.-]+)\s*(>=|<=|==|=|>|<)\s*(\d+)\s*$")

_OPERATORS = {
    "=": lambda actual, want: actual == want,
    "==": lambda actual, want: actual == want,
    ">=": lambda actual, want: actual >= want,
    "<=": lambda actual, want: actual <= want,
    ">": lambda actual, want: actual > want,
    "<": lambda actual, want: actual < want,
}


@dataclass(frozen=True, slots=True)
class Expectation:
    name: str
    operator: str
    value: int

    @classmethod
    def parse(cls, text: str) -> Expectation:
        match = _EXPECTATION_RE.match(text)
        if not match:
            raise ValueError(
                f"unparseable expectation {text!r}; use NAME{{=,>=,<=,>,<}}INT"
            )
        return cls(match.group(1), match.group(2), int(match.group(3)))


@dataclass(frozen=True, slots=True)
class CheckResult:
    expectation: Expectation
    actual: int
    source: str  # funnel | histogram | occurrences

    @property
    def passed(self) -> bool:
        return bool(_OPERATORS[self.expectation.operator](self.actual, self.expectation.value))

    def render(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        exp = self.expectation
        return (
            f"{status} {exp.name}{exp.operator}{exp.value} "
            f"(actual={self.actual}, from {self.source})"
        )


def parse_funnel(text: str) -> dict[str, int]:
    """Counters from the last 'candidate funnel:' line in the log."""
    counters: dict[str, int] = {}
    for match in _FUNNEL_RE.finditer(text):
        counters = {key: int(value) for key, value in _PAIR_RE.findall(match.group("body"))}
    return counters


def parse_histogram(text: str) -> dict[str, int]:
    """Buckets from the last 'rejections by reason:' line in the log."""
    buckets: dict[str, int] = {}
    for match in _HISTOGRAM_RE.finditer(text):
        buckets = {key: int(value) for key, value in _PAIR_RE.findall(match.group("body"))}
    return buckets


def evaluate_expectations(text: str, expectations: list[Expectation]) -> list[CheckResult]:
    funnel = parse_funnel(text)
    histogram = parse_histogram(text)
    histogram_seen = bool(histogram)
    results: list[CheckResult] = []
    for expectation in expectations:
        if expectation.name in funnel:
            results.append(CheckResult(expectation, funnel[expectation.name], "funnel"))
        elif expectation.name in histogram:
            results.append(CheckResult(expectation, histogram[expectation.name], "histogram"))
        elif histogram_seen and expectation.name.endswith("_conflict"):
            # A conflict bucket absent from a present histogram fired 0 times.
            results.append(CheckResult(expectation, 0, "histogram"))
        else:
            results.append(
                CheckResult(expectation, text.count(expectation.name), "occurrences")
            )
    return results


def run_check_log(file: str, expect: list[str]) -> int:
    """CLI body for `arb-scanner check-log`. Returns 0 only if all pass."""
    path = Path(file)
    if not path.is_file():
        print(f"check-log: no such file: {path}")
        return 2
    if not expect:
        print("check-log: no --expect given; nothing to verify")
        return 2
    try:
        expectations = [Expectation.parse(item) for item in expect]
    except ValueError as error:
        print(f"check-log: {error}")
        return 2
    results = evaluate_expectations(path.read_text(encoding="utf-8"), expectations)
    for result in results:
        print(result.render())
    failed = sum(1 for result in results if not result.passed)
    print(f"check-log: {len(results) - failed}/{len(results)} expectations passed")
    return 0 if failed == 0 else 1
