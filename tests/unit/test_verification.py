"""Automated risk-flag verifier tests (matching pipeline stage 6).

The verifier adjudicates the settlement risk flags an accepted pair carries,
using the venue rule text already captured during the scan. It auto-CLEARS
flags it can prove benign (same canonical resolution source, same-date
determination), ACKNOWLEDGES the standard inherent UMA process, FAILS a pair
when it proves a normal-state divergence (different price sources), and leaves
everything else UNRESOLVED for a human. Source text below is verbatim from
docs/VERIFICATION.md and the live fixtures — no invented phrasing.
"""

from datetime import UTC, datetime

from arb_scanner.app.markets.verification import (
    VerificationInputs,
    VerificationStatus,
    VerificationVerdict,
    canonical_resolution_sources,
    verify_determination_time,
    verify_pair,
    verify_resolution_source,
    verify_void_policy,
)

# Verbatim live resolution-source phrasings (docs/VERIFICATION.md §7, §11; live
# Kalshi U-3 and BTC rules fetched 2026-06-21).
POLY_ELECTION_SOURCE = (
    "The resolution source for this market is the Associated Press, Fox News, "
    "and NBC. This market will resolve once all three sources call the race."
)
KALSHI_AP_SOURCE = "This market resolves according to the Associated Press call."
POLY_SPX_SOURCE = (
    "The resolution source for this market is Yahoo Finance, specifically the "
    "S&P 500 (SPX) \"Close\" prices available under \"Historical Prices.\""
)
KALSHI_BTC_COINDESK = "Settles from the Coindesk BTC price index at 16:00 UTC."
POLY_BTC_BINANCE = "This market resolves based on the Binance BTCUSDT price."
KALSHI_U3_BLS = (
    "If the seasonally adjusted unemployment rate (U-3) reported by the Bureau "
    "of Labor Statistics in the Employment Situation Report is above 4.4% in "
    "June 2026, then the market resolves to Yes."
)
POLY_U3_BLS = (
    "This market resolves to the U-3 unemployment rate in the BLS Employment "
    "Situation report for June 2026."
)

T0 = datetime(2026, 6, 30, 16, 0, tzinfo=UTC)


class TestCanonicalResolutionSources:
    def test_extracts_election_caller_set(self) -> None:
        assert canonical_resolution_sources(POLY_ELECTION_SOURCE) == frozenset(
            {"associated_press", "fox_news", "nbc"}
        )

    def test_extracts_yahoo_finance(self) -> None:
        assert canonical_resolution_sources(POLY_SPX_SOURCE) == frozenset({"yahoo_finance"})

    def test_extracts_coindesk_and_binance(self) -> None:
        assert canonical_resolution_sources(KALSHI_BTC_COINDESK) == frozenset({"coindesk"})
        assert canonical_resolution_sources(POLY_BTC_BINANCE) == frozenset({"binance"})

    def test_extracts_bls_from_either_phrasing(self) -> None:
        assert "bls" in canonical_resolution_sources(KALSHI_U3_BLS)
        assert "bls" in canonical_resolution_sources(POLY_U3_BLS)

    def test_no_known_source_returns_empty(self) -> None:
        assert canonical_resolution_sources("Lakers beat Celtics tonight.") == frozenset()


class TestVerifyResolutionSource:
    def test_shared_source_is_cleared(self) -> None:
        check = verify_resolution_source(KALSHI_AP_SOURCE, POLY_ELECTION_SOURCE)
        assert check.status is VerificationStatus.CLEARED
        assert "associated_press" in check.evidence

    def test_same_agency_different_phrasing_is_cleared(self) -> None:
        check = verify_resolution_source(KALSHI_U3_BLS, POLY_U3_BLS)
        assert check.status is VerificationStatus.CLEARED

    def test_disjoint_confident_sources_fail(self) -> None:
        # Coindesk vs Binance can disagree near a strike in NORMAL resolution —
        # a genuine break, not a tail risk.
        check = verify_resolution_source(KALSHI_BTC_COINDESK, POLY_BTC_BINANCE)
        assert check.status is VerificationStatus.FAILED
        assert "coindesk" in check.evidence and "binance" in check.evidence

    def test_unextractable_side_is_unresolved(self) -> None:
        check = verify_resolution_source("Resolves per the official result.", POLY_SPX_SOURCE)
        assert check.status is VerificationStatus.UNRESOLVED


class TestVerifyDeterminationTime:
    def test_identical_is_cleared(self) -> None:
        assert verify_determination_time(T0, T0).status is VerificationStatus.CLEARED

    def test_same_day_different_hours_is_cleared(self) -> None:
        later = datetime(2026, 6, 30, 23, 0, tzinfo=UTC)
        assert verify_determination_time(T0, later).status is VerificationStatus.CLEARED

    def test_three_days_apart_is_unresolved(self) -> None:
        far = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)
        assert verify_determination_time(T0, far).status is VerificationStatus.UNRESOLVED

    def test_missing_side_is_unresolved(self) -> None:
        assert verify_determination_time(T0, None).status is VerificationStatus.UNRESOLVED


class TestVerifyVoidPolicy:
    FAIR_VALUE = (
        "If the event is cancelled, Yes holders receive the last traded price "
        "prior to cancellation."
    )
    RESOLVES_OTHER = "If the event is cancelled, this market will resolve to “Other”."

    def test_same_basis_is_cleared(self) -> None:
        assert (
            verify_void_policy(self.FAIR_VALUE, self.FAIR_VALUE).status
            is VerificationStatus.CLEARED
        )

    def test_different_basis_escalates_to_unresolved(self) -> None:
        # A proven cancellation-tail divergence is real but only bites if the
        # event is voided: escalate to a human, do not auto-clear or auto-fail.
        check = verify_void_policy(self.FAIR_VALUE, self.RESOLVES_OTHER)
        assert check.status is VerificationStatus.UNRESOLVED

    def test_unknown_basis_is_unresolved(self) -> None:
        assert (
            verify_void_policy("Resolves to the winner.", "Resolves to the winner.").status
            is VerificationStatus.UNRESOLVED
        )


class TestVerifyPair:
    def _inputs(self, **over: object) -> VerificationInputs:
        defaults: dict[str, object] = {
            "kalshi_rules_text": KALSHI_AP_SOURCE,
            "poly_rules_text": POLY_ELECTION_SOURCE,
            "kalshi_determination_time": T0,
            "poly_determination_time": T0,
        }
        defaults.update(over)
        return VerificationInputs(**defaults)  # type: ignore[arg-type]

    def test_all_benign_flags_verify(self) -> None:
        report = verify_pair(
            (
                "resolution source unverified on at least one venue",
                "UMA challenge window: Polymarket outcome can be disputed post-resolution",
                "determination time differs within window: ...",
            ),
            self._inputs(),
        )
        assert report.verdict is VerificationVerdict.VERIFIED
        assert not report.unresolved()
        assert not report.failures()
        # UMA is acknowledged as the standard inherent process.
        assert any(c.status is VerificationStatus.ACKNOWLEDGED for c in report.checks)

    def test_proven_source_divergence_rejects(self) -> None:
        report = verify_pair(
            ("resolution source differs: ...", "UMA challenge window: ..."),
            self._inputs(
                kalshi_rules_text=KALSHI_BTC_COINDESK,
                poly_rules_text=POLY_BTC_BINANCE,
            ),
        )
        assert report.verdict is VerificationVerdict.REJECTED
        assert report.failures()

    def test_unresolvable_flag_needs_human(self) -> None:
        report = verify_pair(
            (
                "market_type missing on one venue",
                "UMA challenge window: ...",
            ),
            self._inputs(),
        )
        assert report.verdict is VerificationVerdict.NEEDS_HUMAN
        assert report.unresolved()

    def test_no_flags_is_verified(self) -> None:
        assert verify_pair((), self._inputs()).verdict is VerificationVerdict.VERIFIED

    def test_rejected_outranks_needs_human(self) -> None:
        report = verify_pair(
            (
                "resolution source differs: ...",
                "market_type missing on one venue",
            ),
            self._inputs(
                kalshi_rules_text=KALSHI_BTC_COINDESK,
                poly_rules_text=POLY_BTC_BINANCE,
            ),
        )
        assert report.verdict is VerificationVerdict.REJECTED
