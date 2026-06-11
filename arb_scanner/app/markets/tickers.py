"""Conservative Kalshi ticker inference used only for conflicts and diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from arb_scanner.app.markets.parsers import Evidence, EvidenceConfidence, MarketType

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


@dataclass(frozen=True, slots=True)
class TickerInference:
    event_date: Evidence | None = None
    event_year: Evidence | None = None
    category_family: Evidence | None = None
    state: Evidence | None = None
    office: Evidence | None = None
    threshold: Evidence | None = None
    market_type: Evidence | None = None

    def as_dict(self) -> dict[str, dict[str, str] | None]:
        return {
            "event_date": self.event_date.as_dict() if self.event_date else None,
            "event_year": self.event_year.as_dict() if self.event_year else None,
            "category_family": self.category_family.as_dict() if self.category_family else None,
            "state": self.state.as_dict() if self.state else None,
            "office": self.office.as_dict() if self.office else None,
            "threshold": self.threshold.as_dict() if self.threshold else None,
            "market_type": self.market_type.as_dict() if self.market_type else None,
        }


def _evidence(value: str, confidence: EvidenceConfidence = EvidenceConfidence.MEDIUM) -> Evidence:
    return Evidence(value, confidence, "ticker")


def _year(ticker: str) -> int | None:
    match = re.search(r"-(\d{2})(?:[A-Z]{3}\d{1,2}|-)", ticker)
    return 2000 + int(match[1]) if match else None


def _date(ticker: str) -> date | None:
    match = re.search(r"-(\d{2})([A-Z]{3})(\d{1,2})", ticker)
    if not match or match[2] not in _MONTHS:
        return None
    try:
        return date(2000 + int(match[1]), _MONTHS[match[2]], int(match[3]))
    except ValueError:
        return None


def _state(ticker: str) -> str | None:
    for pattern in (
        r"(?:GOV|SENATE|ATTYGEN)([A-Z]{2})",
        r"GOVPARTY([A-Z]{2})",
        r"HOUSE(?:WINSTATE)?-?([A-Z]{2})",
    ):
        match = re.search(pattern, ticker)
        if match and match[1] in _STATES:
            return match[1]
    return None


def parse_kalshi_ticker(ticker: str) -> TickerInference:
    upper = ticker.upper()
    event_date = _date(upper)
    event_year = event_date.year if event_date else _year(upper)
    threshold_match = re.search(r"-T(\d+(?:\.\d+)?)\b", upper)

    family: str | None = None
    office: str | None = None
    market_type: MarketType | None = None
    if "BTC" in upper or "ETH" in upper or "CRYPTO" in upper:
        family = "crypto"
        market_type = MarketType.CRYPTO_PRICE_THRESHOLD if threshold_match else None
    elif any(term in upper for term in ("NASDAQ", "SP500", "SPX", "DJIA")):
        family = "stock_index"
        market_type = MarketType.STOCK_INDEX_PRICE_THRESHOLD if threshold_match else None
    elif any(term in upper for term in ("TEMP", "RAIN", "SNOW", "WEATHER")):
        family = "weather"
        market_type = MarketType.WEATHER_THRESHOLD if threshold_match else None
    elif any(term in upper for term in ("GOV", "SENATE", "HOUSE", "PRES", "ATTYGEN")):
        family = "election"

    nominee = "NOM" in upper
    if "SENATE" in upper:
        office = "senate"
        market_type = MarketType.PARTY_CONTROL if "CONTROL" in upper else MarketType.SENATE_WINNER
    elif "HOUSE" in upper:
        office = "house"
        market_type = MarketType.PARTY_CONTROL if "CONTROL" in upper else MarketType.HOUSE_WINNER
    elif "GOV" in upper:
        office = "governor"
        market_type = market_type or MarketType.GOVERNOR_WINNER
    elif "PRES" in upper:
        office = "president"
        market_type = MarketType.PRESIDENTIAL_WINNER
    elif "ATTYGEN" in upper:
        office = "attorney_general"
        market_type = MarketType.ELECTION_WINNER
    if nominee:
        market_type = MarketType.PARTY_NOMINEE
    if "PRIMARYPLACE" in upper:
        market_type = MarketType.PRIMARY_PLACEMENT
    if "ADVANCE" in upper:
        market_type = MarketType.PRIMARY_ADVANCEMENT
    if any(term in upper for term in ("MLS", "NFL", "NBA", "NHL", "MLB", "WNBA")):
        family = "sports"
        market_type = MarketType.SPORTS_MONEYLINE

    return TickerInference(
        event_date=(
            _evidence(event_date.isoformat(), EvidenceConfidence.HIGH) if event_date else None
        ),
        event_year=_evidence(str(event_year)) if event_year else None,
        category_family=_evidence(family) if family else None,
        state=_evidence(value) if (value := _state(upper)) else None,
        office=_evidence(office) if office else None,
        threshold=(
            _evidence(str(Decimal(threshold_match[1])), EvidenceConfidence.HIGH)
            if threshold_match
            else None
        ),
        market_type=_evidence(market_type.value) if market_type else None,
    )
