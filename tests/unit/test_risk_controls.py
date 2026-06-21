"""Risk-control gate tests: each control individually vetoes an alert."""

from decimal import Decimal
from pathlib import Path

from arb_scanner.app.risk.controls import OpportunityRisk, RiskLimits, check
from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import Money, Venue

D = Decimal


def good_opp(**overrides: object) -> OpportunityRisk:
    defaults: dict[str, object] = {
        "locked_capital": Money.from_dollars("100"),
        "net_profit": Money.from_dollars("10"),
        "simple_return": D("0.10"),
        "annualized_return": D("1.2"),
        "match_confidence": 0.95,
        "fill_fraction": D(1),
        "hold_days": D(30),
        "quote_age_seconds": 1.0,
        "category": "crypto",
    }
    defaults.update(overrides)
    return OpportunityRisk(**defaults)  # type: ignore[arg-type]


def test_clean_opportunity_passes() -> None:
    assert check(good_opp(), RiskLimits(), ExposureTracker(), KillSwitch()) == []


def test_kill_switch_blocks() -> None:
    switch = KillSwitch()
    switch.engage()
    reasons = check(good_opp(), RiskLimits(), ExposureTracker(), switch)
    assert any("kill switch" in r for r in reasons)


def test_kill_switch_file_flag(tmp_path: Path) -> None:
    flag = tmp_path / "KILL"
    switch = KillSwitch(flag_file=flag)
    assert check(good_opp(), RiskLimits(), ExposureTracker(), switch) == []
    flag.touch()
    assert any(
        "kill switch" in r for r in check(good_opp(), RiskLimits(), ExposureTracker(), switch)
    )


def test_per_trade_exposure() -> None:
    reasons = check(
        good_opp(locked_capital=Money.from_dollars("2000")),
        RiskLimits(),
        ExposureTracker(),
        KillSwitch(),
    )
    assert any("trade exposure" in r for r in reasons)


def test_venue_exposure_accumulates() -> None:
    tracker = ExposureTracker()
    tracker.add(Venue.KALSHI, Money.from_dollars("4950"))
    reasons = check(good_opp(), RiskLimits(), tracker, KillSwitch())
    assert any("venue exposure" in r for r in reasons)


def test_min_net_profit() -> None:
    reasons = check(
        good_opp(net_profit=Money.from_dollars("1")), RiskLimits(), ExposureTracker(), KillSwitch()
    )
    assert any("net $" in r for r in reasons)


def test_min_returns() -> None:
    reasons = check(
        good_opp(simple_return=D("0.001"), annualized_return=D("0.01")),
        RiskLimits(),
        ExposureTracker(),
        KillSwitch(),
    )
    assert any("ROI" in r for r in reasons)
    assert any("annualized" in r for r in reasons)


def test_confidence_fill_hold_age() -> None:
    reasons = check(
        good_opp(
            match_confidence=0.5,
            fill_fraction=D("0.6"),
            hold_days=D(180),
            quote_age_seconds=120.0,
        ),
        RiskLimits(),
        ExposureTracker(),
        KillSwitch(),
    )
    joined = " ".join(reasons)
    assert "confidence" in joined
    assert "fill fraction" in joined
    assert "hold" in joined
    assert "quote age" in joined


def test_unknown_hold_and_quote_age_fail_closed() -> None:
    reasons = check(
        good_opp(hold_days=None, quote_age_seconds=None),
        RiskLimits(),
        ExposureTracker(),
        KillSwitch(),
    )
    assert "hold time unknown" in reasons
    assert "quote age unknown" in reasons


def test_category_allowlist() -> None:
    limits = RiskLimits(category_allowlist=frozenset({"sports"}))
    reasons = check(good_opp(category="crypto"), limits, ExposureTracker(), KillSwitch())
    assert any("allowlist" in r for r in reasons)


def test_implausible_edge_blocks_likely_non_equivalent_match() -> None:
    # Two genuinely-equivalent binary markets in liquid venues never show a
    # ~50¢/share guaranteed edge: an implausibly large edge is evidence the
    # pair is NOT the same event (false match), so it must not alert.
    reasons = check(
        good_opp(gross_edge_per_share=D("0.50")),
        RiskLimits(),
        ExposureTracker(),
        KillSwitch(),
    )
    assert any("implausible" in r.lower() for r in reasons)


def test_plausible_edge_passes() -> None:
    # A realistic cross-venue arb edge (single-digit ¢/share) is allowed.
    assert (
        check(
            good_opp(gross_edge_per_share=D("0.08")),
            RiskLimits(),
            ExposureTracker(),
            KillSwitch(),
        )
        == []
    )
