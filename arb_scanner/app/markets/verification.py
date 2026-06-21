"""Automated risk-flag verifier (matching pipeline stage 6).

An accepted pair carries ``risk_flags`` — settlement-mechanic caveats a human
would otherwise verify before treating the alert as an opportunity. This module
adjudicates those flags against the venue rule text already captured during the
scan, to take the human out of the loop for the routine cases:

- CLEARED      proven benign (same canonical resolution source, same-date
               determination): no human action needed.
- ACKNOWLEDGED an accepted, standard, inherent risk (the Polymarket UMA
               challenge window applies uniformly): recorded, no action.
- FAILED       proven NORMAL-state divergence (e.g. two different price index
               sources that can disagree near a strike): the pair is not a safe
               same-event hedge, so its alert is suppressed.
- UNRESOLVED   the flag could not be auto-decided from available evidence:
               escalate to a human, who only needs to check these.

Verdict: any FAILED -> REJECTED; else any UNRESOLVED -> NEEDS_HUMAN; else
VERIFIED. The verifier is conservative — absence of evidence is UNRESOLVED,
never CLEARED, and it never makes a pair look safer than the evidence supports.
It is still discovery-only and never authorizes a trade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from arb_scanner.app.markets.rule_equivalence import cancellation_policy_basis

# A determination-time gap at or under this is a benign intraday/buffer
# difference (trading cutoff vs UMA end date); the rule layer already rejects
# materially different horizons (>7 days) before a pair ever reaches here.
_DETERMINATION_CLEAR_WINDOW = timedelta(hours=24)


class VerificationStatus(StrEnum):
    CLEARED = "cleared"
    ACKNOWLEDGED = "acknowledged"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class VerificationVerdict(StrEnum):
    VERIFIED = "verified"  # every flag cleared or acknowledged
    NEEDS_HUMAN = "needs_human"  # some flag could not be auto-decided
    REJECTED = "rejected"  # a flag proved a normal-state divergence


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    flag: str
    status: VerificationStatus
    evidence: str


@dataclass(frozen=True, slots=True)
class VerificationInputs:
    kalshi_rules_text: str
    poly_rules_text: str
    kalshi_determination_time: datetime | None
    poly_determination_time: datetime | None


@dataclass(frozen=True, slots=True)
class VerificationReport:
    verdict: VerificationVerdict
    checks: tuple[VerificationCheck, ...]

    def _by_status(self, status: VerificationStatus) -> tuple[VerificationCheck, ...]:
        return tuple(check for check in self.checks if check.status is status)

    def cleared(self) -> tuple[VerificationCheck, ...]:
        return self._by_status(VerificationStatus.CLEARED)

    def acknowledged(self) -> tuple[VerificationCheck, ...]:
        return self._by_status(VerificationStatus.ACKNOWLEDGED)

    def unresolved(self) -> tuple[VerificationCheck, ...]:
        return self._by_status(VerificationStatus.UNRESOLVED)

    def failures(self) -> tuple[VerificationCheck, ...]:
        return self._by_status(VerificationStatus.FAILED)

    def summary(self) -> str:
        parts = [f"verdict={self.verdict.value}"]
        for label, checks in (
            ("cleared", self.cleared()),
            ("acknowledged", self.acknowledged()),
            ("needs_human", self.unresolved()),
            ("failed", self.failures()),
        ):
            if checks:
                parts.append(f"{label}={len(checks)}")
        return " ".join(parts)


# Curated, conservative resolution-source patterns. Each maps a canonical token
# to phrasings seen in live venue rules; patterns are deliberately narrow so
# unrelated text never matches. Two markets share a source when their canonical
# sets intersect.
_RESOLUTION_SOURCES: tuple[tuple[str, str], ...] = (
    ("associated_press", r"\bassociated press\b|\bAP\b"),
    ("fox_news", r"\bfox news\b"),
    ("nbc", r"\bNBC\b"),
    ("abc_news", r"\bABC News\b"),
    ("cbs_news", r"\bCBS News\b"),
    ("cnn", r"\bCNN\b"),
    ("decision_desk", r"\bdecision desk\b"),
    ("yahoo_finance", r"\byahoo finance\b"),
    ("coindesk", r"\bcoindesk\b"),
    ("coinbase", r"\bcoinbase\b"),
    ("binance", r"\bbinance\b"),
    ("kraken", r"\bkraken\b"),
    ("cme_cf", r"\bcme cf\b|\bcf benchmarks\b"),
    ("bls", r"\bbureau of labor statistics\b|\bBLS\b|\bemployment situation\b"),
    ("bea", r"\bbureau of economic analysis\b|\bBEA\b"),
    ("federal_reserve", r"\bfederal reserve\b|\bFOMC\b"),
    ("espn", r"\bESPN\b"),
    ("official_league", r"\bofficial league\b|\bleague statistics\b|\bofficial standings\b"),
)


def canonical_resolution_sources(text: str) -> frozenset[str]:
    """Canonical resolution-source tokens named in rule text (possibly empty)."""
    return frozenset(
        name for name, pattern in _RESOLUTION_SOURCES if re.search(pattern, text, re.IGNORECASE)
    )


def verify_resolution_source(kalshi_text: str, poly_text: str) -> VerificationCheck:
    """CLEARED if both name an overlapping source; FAILED if confidently disjoint."""
    flag = "resolution source"
    kalshi = canonical_resolution_sources(kalshi_text)
    poly = canonical_resolution_sources(poly_text)
    if not kalshi or not poly:
        return VerificationCheck(
            flag,
            VerificationStatus.UNRESOLVED,
            f"source not extractable on one venue (kalshi={sorted(kalshi) or 'unknown'}, "
            f"polymarket={sorted(poly) or 'unknown'})",
        )
    if kalshi & poly:
        return VerificationCheck(
            flag,
            VerificationStatus.CLEARED,
            f"shared source {sorted(kalshi & poly)}",
        )
    return VerificationCheck(
        flag,
        VerificationStatus.FAILED,
        f"different sources kalshi={sorted(kalshi)} polymarket={sorted(poly)} — "
        "can diverge in normal resolution",
    )


def verify_determination_time(
    kalshi_time: datetime | None, poly_time: datetime | None
) -> VerificationCheck:
    """CLEARED when both resolve on the same day / within a buffer; else UNRESOLVED."""
    flag = "determination time"
    if kalshi_time is None or poly_time is None:
        return VerificationCheck(
            flag, VerificationStatus.UNRESOLVED, "determination time missing on one venue"
        )
    delta = abs(kalshi_time - poly_time)
    if kalshi_time.date() == poly_time.date() or delta <= _DETERMINATION_CLEAR_WINDOW:
        return VerificationCheck(
            flag,
            VerificationStatus.CLEARED,
            f"both resolve within {max(delta, timedelta(0))} (kalshi={kalshi_time.isoformat()}, "
            f"polymarket={poly_time.isoformat()})",
        )
    return VerificationCheck(
        flag,
        VerificationStatus.UNRESOLVED,
        f"determination times {delta} apart — verify the resolution timing",
    )


def verify_void_policy(kalshi_text: str, poly_text: str) -> VerificationCheck:
    """CLEARED if both prove the same cancellation basis; else UNRESOLVED.

    A proven *different* basis (fair value vs resolves-to-other) only bites in
    the cancellation tail, so it escalates to a human rather than auto-failing
    the otherwise-equivalent pair.
    """
    flag = "void policy"
    kalshi = cancellation_policy_basis(kalshi_text)
    poly = cancellation_policy_basis(poly_text)
    if kalshi is not None and poly is not None and kalshi == poly:
        return VerificationCheck(flag, VerificationStatus.CLEARED, f"both {kalshi}")
    if kalshi is not None and poly is not None:
        return VerificationCheck(
            flag,
            VerificationStatus.UNRESOLVED,
            f"cancellation basis differs (kalshi={kalshi}, polymarket={poly}) — "
            "only affects the void tail",
        )
    return VerificationCheck(
        flag,
        VerificationStatus.UNRESOLVED,
        f"cancellation basis not provable (kalshi={kalshi or 'unknown'}, "
        f"polymarket={poly or 'unknown'})",
    )


def _verify_flag(flag: str, inputs: VerificationInputs) -> VerificationCheck:
    lowered = flag.lower()
    if "uma" in lowered:
        return VerificationCheck(
            flag,
            VerificationStatus.ACKNOWLEDGED,
            "standard Polymarket UMA challenge window — inherent and uniform",
        )
    if "resolution source" in lowered:
        return _retag(
            verify_resolution_source(inputs.kalshi_rules_text, inputs.poly_rules_text), flag
        )
    if "determination time" in lowered:
        return _retag(
            verify_determination_time(
                inputs.kalshi_determination_time, inputs.poly_determination_time
            ),
            flag,
        )
    if "void" in lowered:
        return _retag(verify_void_policy(inputs.kalshi_rules_text, inputs.poly_rules_text), flag)
    # Resolution text, source-finalization, tie policy, candidate/magnitude
    # mismatches, presence gaps, sports timing, ticker-only inference: real but
    # not auto-decidable here — a human checks these.
    return VerificationCheck(flag, VerificationStatus.UNRESOLVED, "not auto-verifiable")


def _retag(check: VerificationCheck, flag: str) -> VerificationCheck:
    """Carry the originating flag text on the resulting check."""
    return VerificationCheck(flag, check.status, check.evidence)


def verify_pair(risk_flags: tuple[str, ...], inputs: VerificationInputs) -> VerificationReport:
    """Adjudicate every risk flag and roll the checks up to one verdict."""
    checks = tuple(_verify_flag(flag, inputs) for flag in risk_flags)
    if any(check.status is VerificationStatus.FAILED for check in checks):
        verdict = VerificationVerdict.REJECTED
    elif any(check.status is VerificationStatus.UNRESOLVED for check in checks):
        verdict = VerificationVerdict.NEEDS_HUMAN
    else:
        verdict = VerificationVerdict.VERIFIED
    return VerificationReport(verdict=verdict, checks=checks)
