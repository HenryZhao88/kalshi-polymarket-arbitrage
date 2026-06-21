"""Risk-control gate (SPEC Phase 4): every control must pass before any alert."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.types import Money, Venue


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_exposure_total: Money = field(default_factory=lambda: Money.from_dollars("10000"))
    max_exposure_per_venue: Money = field(default_factory=lambda: Money.from_dollars("5000"))
    max_exposure_per_trade: Money = field(default_factory=lambda: Money.from_dollars("1000"))
    min_net_profit: Money = field(default_factory=lambda: Money.from_dollars("5"))
    min_simple_return: Decimal = Decimal("0.01")
    min_annualized_return: Decimal = Decimal("0.10")
    min_match_confidence: float = 0.9
    min_fill_fraction: Decimal = Decimal("1.0")
    max_hold_days: Decimal = Decimal(90)
    max_quote_age_seconds: float = 30.0
    category_allowlist: frozenset[str] | None = None  # None = all categories
    allow_unknown_hold_time: bool = False
    allow_unknown_quote_age: bool = False
    # Two genuinely-equivalent binary markets in liquid venues never trade with
    # a large guaranteed cross-venue edge. A gross edge per share above this cap
    # is far more likely a false match (e.g. a Kalshi total-points line matched
    # to the wrong Polymarket line) than a real arbitrage, so it must not alert.
    max_plausible_edge_per_share: Decimal = Decimal("0.15")


@dataclass(frozen=True, slots=True)
class OpportunityRisk:
    """Risk-relevant facts of one evaluated opportunity."""

    locked_capital: Money
    net_profit: Money
    simple_return: Decimal
    annualized_return: Decimal
    match_confidence: float
    fill_fraction: Decimal
    hold_days: Decimal | None
    quote_age_seconds: float | None
    category: str | None
    #: Gross edge per share = 1 − leg1_vwap − leg2_vwap (pre-fee). A large value
    #: signals the two markets are not actually the same event.
    gross_edge_per_share: Decimal = Decimal(0)


def check(
    opp: OpportunityRisk,
    limits: RiskLimits,
    exposure: ExposureTracker,
    kill_switch: KillSwitch,
) -> list[str]:
    """All rejection reasons (empty list = alertable)."""
    reasons: list[str] = []
    if kill_switch.engaged:
        reasons.append("kill switch engaged")
    if opp.locked_capital > limits.max_exposure_per_trade:
        reasons.append(
            f"trade exposure {opp.locked_capital.to_dollars()} > "
            f"{limits.max_exposure_per_trade.to_dollars()}"
        )
    for venue in (Venue.KALSHI, Venue.POLYMARKET):
        if exposure.venue_total(venue) + opp.locked_capital > limits.max_exposure_per_venue:
            reasons.append(f"venue exposure limit ({venue}) exceeded")
    if exposure.total() + opp.locked_capital > limits.max_exposure_total:
        reasons.append("total exposure limit exceeded")
    if opp.net_profit < limits.min_net_profit:
        reasons.append(
            f"net ${opp.net_profit.to_dollars()} < min ${limits.min_net_profit.to_dollars()}"
        )
    if opp.simple_return < limits.min_simple_return:
        reasons.append(f"ROI {opp.simple_return:.4f} < min {limits.min_simple_return}")
    if opp.annualized_return < limits.min_annualized_return:
        reasons.append(
            f"annualized {opp.annualized_return:.4f} < min {limits.min_annualized_return}"
        )
    if opp.match_confidence < limits.min_match_confidence:
        reasons.append(f"confidence {opp.match_confidence} < min {limits.min_match_confidence}")
    if opp.fill_fraction < limits.min_fill_fraction:
        reasons.append(f"fill fraction {opp.fill_fraction} < min {limits.min_fill_fraction}")
    if opp.hold_days is None and not limits.allow_unknown_hold_time:
        reasons.append("hold time unknown")
    elif opp.hold_days is not None and opp.hold_days > limits.max_hold_days:
        reasons.append(f"hold {opp.hold_days}d > max {limits.max_hold_days}d")
    if opp.quote_age_seconds is None and not limits.allow_unknown_quote_age:
        reasons.append("quote age unknown")
    elif opp.quote_age_seconds is not None and opp.quote_age_seconds > limits.max_quote_age_seconds:
        reasons.append(
            f"quote age {opp.quote_age_seconds:.1f}s > max {limits.max_quote_age_seconds}s"
        )
    if limits.category_allowlist is not None and (
        opp.category is None or opp.category.lower() not in limits.category_allowlist
    ):
        reasons.append(f"category {opp.category!r} not in allowlist")
    if opp.gross_edge_per_share > limits.max_plausible_edge_per_share:
        reasons.append(
            f"implausible edge {opp.gross_edge_per_share}/share > "
            f"{limits.max_plausible_edge_per_share} — likely non-equivalent markets, "
            "not a real arbitrage"
        )
    return reasons
