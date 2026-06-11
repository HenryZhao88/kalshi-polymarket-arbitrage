"""Polymarket fee functions. Pure; all monetary returns are `Money` (USDC ≈ USD).

Sources (retrieved 2026-06-11, see docs/VERIFICATION.md §2):
- Formula, category rates, rounding: https://docs.polymarket.com/trading/fees
- Generalized (rate, exponent) form: official SDK
  https://github.com/Polymarket/py-clob-client-v2 (py_clob_client_v2/fees.py)
- Per-market resolution: WS `new_market.fee_schedule` / get_clob_market_info;
  /fee-rate `base_fee` is a protocol cap, never used alone (VERIFICATION.md §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from arb_scanner.app.types import Money

#: Official category taker rates, https://docs.polymarket.com/trading/fees (2026-06-11).
#: Geopolitics is fee-free. Used only as a flagged fallback when per-market metadata
#: is unavailable.
CATEGORY_TAKER_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.03"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "geopolitics": Decimal(0),
}

#: "Fees are rounded to 5 decimal places. The smallest fee charged is 0.00001 USDC."
FEE_QUANTUM = Decimal("0.00001")


class FeeRateSource(StrEnum):
    MARKET_METADATA = "market_metadata"  # fee_schedule from WS/market info — trusted
    CATEGORY_DEFAULT = "category_default"  # flagged fallback, unverified per market
    UNKNOWN = "unknown"  # no information — never price


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    rate: Decimal
    exponent: Decimal
    source: FeeRateSource
    taker_only: bool = True


def polymarket_taker_fee_raw(
    shares: Decimal, price: Decimal, rate: Decimal, exponent: Decimal
) -> Decimal:
    """Unrounded taker fee in USDC: shares × rate × (p(1−p))^exponent.

    Docs formula (https://docs.polymarket.com/trading/fees, 2026-06-11) is the
    exponent=1 case: fee = C × feeRate × p × (1−p). The generalized form matches the
    official SDK. Makers pay zero.
    Worked SDK vector: 100 shares, p=0.5, rate=0.25, exponent=2 → 1.5625.
    """
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError(f"price {price} outside [0, 1]")
    pq = price * (Decimal(1) - price)
    # Decimal ** Decimal needs integral exponents to stay exact; non-integral
    # exponents have not been observed in the wild (VERIFICATION.md §2.2).
    if exponent == exponent.to_integral_value():
        impact = pq ** int(exponent)
    else:
        raise ValueError(f"non-integral fee exponent {exponent} is unsupported")
    return shares * rate * impact


def polymarket_taker_fee(
    shares: Decimal, price: Decimal, rate: Decimal, exponent: Decimal = Decimal(1)
) -> Money:
    """Taker fee rounded to 5 decimals; minimum charged fee 0.00001 USDC.

    "Anything smaller rounds to zero" (https://docs.polymarket.com/trading/fees,
    2026-06-11). Half-up at the 5th decimal is our assumption — the page does not
    name the rounding mode (flagged in docstring per project convention).
    Worked example: 100 shares, p=0.03, rate=0.04 → 0.1164 → $0.11640.
    """
    raw = polymarket_taker_fee_raw(shares, price, rate, exponent)
    rounded = raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
    return Money.from_dollars(rounded)


def resolve_fee_schedule(market_schedule: FeeSchedule | None, category: str | None) -> FeeSchedule:
    """Resolve the effective fee schedule for a market.

    Order (VERIFICATION.md §2.2): per-market metadata → category default (flagged) →
    UNKNOWN (never priced). /fee-rate base_fee is deliberately not an input.
    """
    if market_schedule is not None:
        return market_schedule
    if category is not None:
        rate = CATEGORY_TAKER_RATES.get(category.lower(), CATEGORY_TAKER_RATES["other"])
        return FeeSchedule(rate=rate, exponent=Decimal(1), source=FeeRateSource.CATEGORY_DEFAULT)
    return FeeSchedule(rate=Decimal(0), exponent=Decimal(1), source=FeeRateSource.UNKNOWN)


def maker_rebate(taker_fees_collected: Money, rebate_rate: Decimal) -> Money:
    """Optional maker rebate: share of collected taker fees, paid daily in USDC.

    25% for most categories, 20% crypto (https://docs.polymarket.com/trading/fees,
    2026-06-11). User-specific and OFF by default; never counted in headline edge —
    FeeBreakdown keeps it in a separate excluded field.
    """
    raw = taker_fees_collected.to_dollars() * rebate_rate
    return Money.from_dollars(raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP))
