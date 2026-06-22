"""Conservative title/rules parsing for matching and review diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

_NOISE_WORDS = frozenset(
    {"will", "the", "a", "an", "be", "by", "in", "on", "at", "to", "of", "this", "is"}
)

_STRIKE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "money",
        re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(k)?", re.IGNORECASE),
    ),
    ("percentage", re.compile(r"\b(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)),
    (
        "percentage_points",
        re.compile(r"\b(\d+(?:\.\d+)?)\s+percentage points?\b", re.IGNORECASE),
    ),
    (
        "sports_line",
        re.compile(
            r"\b(?:spread|line|handicap)\s*(?:of|is|at)?\s*([+-]?\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
    ("thousands", re.compile(r"\b(\d+(?:\.\d+)?)k\b", re.IGNORECASE)),
    (
        "directional",
        re.compile(
            r"\b(?:above|below|over|under|at least|at most|reach(?:es)?|exceed(?:s)?)\s+"
            r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
)
_DIRECTION_RE = re.compile(
    r"\b(above|below|over|under|at least|at most|reach(?:es)?|exceed(?:s)?)\b", re.I
)
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_MONTH_DATE_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|sept|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DIRECTION_CANON = {
    "above": "above",
    "over": "above",
    "at least": "above",
    "reach": "above",
    "reaches": "above",
    "exceed": "above",
    "exceeds": "above",
    "below": "below",
    "under": "below",
    "at most": "below",
}

_CRYPTO_RE = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto)\b", re.I)
_STOCK_INDEX_RE = re.compile(
    r"\b(s&p(?: 500)?|spx|nasdaq(?: 100)?|dow|djia|stock|share price|index)\b", re.I
)
_WEATHER_RE = re.compile(
    r"\b(temperature|degrees?|rain(?:fall)?|snow(?:fall)?|wind|hurricane|weather)\b", re.I
)
_SPORTS_HINT_RE = re.compile(
    r"\b(game|match|series|team|points?|runs?|goals?|moneyline|spread|total|cover|"
    r"champion(?:ship)?|cup|league|cricket|fc|usl|nfl|nba|nhl|mlb|mls|wnba|ncaa)\b",
    re.I,
)
# Tournament stage team-count contracts ("at least N teams from X reach the
# knockout stage") are a different bet from any winner/moneyline contract.
_STAGE_COUNT_RE = re.compile(r"\bknockout (?:stage|round)\b|\bround of (?:16|32)\b", re.I)
_TEAM_COUNT_RE = re.compile(
    r"\b(?:at least|exactly|more than|fewer than)\b[^.\n]{0,20}\bteams?\b|\bteams? from\b",
    re.I,
)
# Crypto best-month / monthly-performance contracts resolve on relative
# monthly returns, never on a price threshold.
_CRYPTO_MONTHLY_RE = re.compile(
    r"\bbest(?:[- ]performing)? month\b|\bhighest percentage change\b|"
    r"\bmonthly candle\b|\bmonthly performance\b|\bworst month\b",
    re.I,
)
# Continent-level World Cup winner contracts are a different bet from a
# country-level winner contract: typing them distinctly makes the pairing
# evidence explicit.
_WC_CONTINENT_WINNER_RE = re.compile(r"\bworld cup\b", re.I)
_CONTINENT_NAME_RE = re.compile(
    r"\bsouth\s+america\b|\bnorth\s+america\b|\beurope\b|\bafrica\b|\basia\b|\boceania\b",
    re.I,
)
# All-of candidate sweep contracts: a named slate or cohort must all win
# their primaries/nominating races. Never the same bet as a single race.
_CANDIDATE_SWEEP_RE = re.compile(
    r"\ball\b[^.\n]{0,60}\bwin\b|\bwin(?:s)?\s+all\b|\ball of the following\b", re.I
)
_CANDIDATE_COHORT_RE = re.compile(r"\b(?:candidates?|incumbents?)\b", re.I)


# Single source of truth for US state names (lowercase) and their postal
# abbreviations. Multi-word names must stay intact in regexes built from
# these so e.g. "North Carolina" never matches a bare "Carolina".
US_STATE_ABBREVIATIONS: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
US_STATE_NAMES: tuple[str, ...] = tuple(US_STATE_ABBREVIATIONS)


class EvidenceConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MarketType(StrEnum):
    ELECTION_WINNER = "election_winner"
    PARTY_NOMINEE = "party_nominee"
    PRIMARY_PLACEMENT = "primary_placement"
    PRIMARY_ADVANCEMENT = "primary_advancement"
    GOVERNOR_WINNER = "governor_winner"
    SENATE_WINNER = "senate_winner"
    HOUSE_WINNER = "house_winner"
    PRESIDENTIAL_WINNER = "presidential_winner"
    PARTY_CONTROL = "party_control"
    MARGIN_SPREAD = "margin_spread"
    EXACT_VOTE_SHARE = "exact_vote_share"
    TURNOUT = "turnout"
    OFFICE_HOLDER = "office_holder_confirmation_appointment"
    ELECTION_CANDIDATE_SWEEP = "election_candidate_sweep"
    SPORTS_MONEYLINE = "sports_moneyline"
    SPORTS_SPREAD = "sports_spread"
    SPORTS_TOTAL = "sports_total"
    SPORTS_STAGE_COUNT = "sports_stage_count"
    SPORTS_WC_CONTINENT_WINNER = "sports_world_cup_continent_winner"
    CRYPTO_PRICE_THRESHOLD = "crypto_price_threshold"
    CRYPTO_EXACT_PRICE = "crypto_exact_price"
    CRYPTO_MONTHLY_PERFORMANCE = "crypto_monthly_performance"
    STOCK_INDEX_PRICE_THRESHOLD = "stock_index_price_threshold"
    STOCK_INDEX_EXACT_PRICE = "stock_index_exact_price"
    WEATHER_THRESHOLD = "weather_threshold"


@dataclass(frozen=True, slots=True)
class Evidence:
    value: str
    confidence: EvidenceConfidence
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "confidence": self.confidence.value,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ParsedFeatures:
    """Structured fields extracted from text; absence is not guessed around."""

    normalized_title: str
    strike: Decimal | None = None
    direction: str | None = None
    event_date: date | None = None
    event_year: int | None = None
    market_type: MarketType | None = None
    strike_evidence: Evidence | None = None
    event_date_evidence: Evidence | None = None
    event_year_evidence: Evidence | None = None
    market_type_evidence: Evidence | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)


def normalize_title(title: str) -> str:
    """Lowercase, remove punctuation/noise, and expand numeric ``k`` suffixes."""
    text = title.lower()
    text = re.sub(r"\$(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m[1]) * 1000)), text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m[1]) * 1000)), text)
    text = text.replace(",", "")
    text = re.sub(r"[^\w\s.]", " ", text)
    text = re.sub(r"\.(?=\s|$)", "", text)
    tokens = [token for token in text.split() if token not in _NOISE_WORDS]
    return " ".join(tokens)


def _month_number(value: str) -> int:
    return _MONTHS[value.lower().rstrip(".")[:4].rstrip("t")[:3]]


def extract_event_date(text: str, reference_time: datetime | None = None) -> date | None:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    slash = _SLASH_DATE_RE.search(text)
    if slash:
        try:
            return date(int(slash[3]), int(slash[1]), int(slash[2]))
        except ValueError:
            return None
    named = _MONTH_DATE_RE.search(text)
    if not named:
        return None
    year = int(named[3]) if named[3] else (reference_time.year if reference_time else None)
    if year is None:
        return None
    try:
        return date(year, _month_number(named[1]), int(named[2]))
    except (KeyError, ValueError):
        return None


def extract_event_year(text: str, event_date: date | None = None) -> int | None:
    if event_date is not None:
        return event_date.year
    match = _YEAR_RE.search(text)
    return int(match[1]) if match else None


def _extract_strike_with_kind(text: str) -> tuple[Decimal | None, str | None]:
    for kind, pattern in _STRIKE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = Decimal(match[1].replace(",", ""))
        has_k_suffix = kind in {"money", "thousands"} and (
            kind == "thousands" or (match.lastindex == 2 and bool(match[2]))
        )
        return (value * 1000 if has_k_suffix else value), kind
    return None, None


def extract_strike(text: str) -> Decimal | None:
    return _extract_strike_with_kind(text)[0]


def infer_market_type(
    text: str,
    *,
    category: str | None = None,
    source: str = "title",
) -> Evidence | None:
    lowered = text.lower()
    category_lower = (category or "").lower()
    is_sports = category_lower == "sports" or bool(_SPORTS_HINT_RE.search(text))

    if re.search(r"\b(qualify|advance)\b", lowered) and re.search(r"\b(primary|runoff)\b", lowered):
        value = MarketType.PRIMARY_ADVANCEMENT
    elif re.search(
        r"\bfinish(?:es)?\s+(?:1st|2nd|3rd|first|second|third)\b", lowered
    ) and re.search(r"\bprimary\b", lowered):
        value = MarketType.PRIMARY_PLACEMENT
    elif re.search(r"\b(nominee|nomination|primary winner)\b", lowered):
        value = MarketType.PARTY_NOMINEE
    elif (
        _CANDIDATE_SWEEP_RE.search(lowered)
        and _CANDIDATE_COHORT_RE.search(lowered)
        and re.search(r"\b(primar\w+|nominat\w+|election\w*)\b", lowered)
    ):
        value = MarketType.ELECTION_CANDIDATE_SWEEP
    elif re.search(r"\b(control|majority)\b", lowered) and re.search(
        r"\b(senate|house|congress|legislature|party)\b", lowered
    ):
        value = MarketType.PARTY_CONTROL
    elif re.search(
        r"\b(turnout|votes cast|number of voters|total vote count|total votes)\b",
        lowered,
    ):
        value = MarketType.TURNOUT
    elif re.search(r"\b(exact(?:ly)?|between)\b", lowered) and re.search(
        r"\b(vote share|percent(?:age)? of (?:the )?vote)\b", lowered
    ):
        value = MarketType.EXACT_VOTE_SHARE
    elif re.search(r"\b(margin of victory|vote margin|percentage points?)\b", lowered):
        value = MarketType.MARGIN_SPREAD
    elif re.search(r"\b(confirmed|confirmation|appointed|appointment|office holder)\b", lowered):
        value = MarketType.OFFICE_HOLDER
    elif is_sports and _STAGE_COUNT_RE.search(lowered) and _TEAM_COUNT_RE.search(lowered):
        value = MarketType.SPORTS_STAGE_COUNT
    elif (
        _WC_CONTINENT_WINNER_RE.search(lowered)
        and _CONTINENT_NAME_RE.search(lowered)
        and re.search(r"\b(win|wins|winner)\b", lowered)
    ):
        value = MarketType.SPORTS_WC_CONTINENT_WINNER
    elif is_sports and re.search(r"\b(spread|cover|handicap|win by)\b", lowered):
        value = MarketType.SPORTS_SPREAD
    elif (
        is_sports
        and re.search(r"\b(total|combined)\b", lowered)
        and re.search(r"\b(points?|runs?|goals?|games?|sets?)\b", lowered)
    ):
        value = MarketType.SPORTS_TOTAL
    elif is_sports and re.search(r"\b(win|wins|beat|moneyline)\b", lowered):
        value = MarketType.SPORTS_MONEYLINE
    elif _CRYPTO_RE.search(text) and _CRYPTO_MONTHLY_RE.search(lowered):
        value = MarketType.CRYPTO_MONTHLY_PERFORMANCE
    elif _CRYPTO_RE.search(text) and re.search(r"\b(exact(?:ly)?|equal to|close at)\b", lowered):
        value = MarketType.CRYPTO_EXACT_PRICE
    elif _CRYPTO_RE.search(text) and extract_strike(text) is not None:
        value = MarketType.CRYPTO_PRICE_THRESHOLD
    elif _STOCK_INDEX_RE.search(text) and re.search(
        r"\b(exact(?:ly)?|equal to|close at)\b", lowered
    ):
        value = MarketType.STOCK_INDEX_EXACT_PRICE
    elif _STOCK_INDEX_RE.search(text) and extract_strike(text) is not None:
        value = MarketType.STOCK_INDEX_PRICE_THRESHOLD
    elif _WEATHER_RE.search(text) and extract_strike(text) is not None:
        value = MarketType.WEATHER_THRESHOLD
    elif re.search(r"\b(president|presidential|presidency)\b", lowered) and re.search(
        r"\b(win|winner|elected|election)\b", lowered
    ):
        value = MarketType.PRESIDENTIAL_WINNER
    elif re.search(r"\b(governor|governorship|gubernatorial)\b", lowered) and re.search(
        r"\b(win|winner|race|elected|election)\b", lowered
    ):
        value = MarketType.GOVERNOR_WINNER
    elif re.search(r"\b(senate|senator|senatorial)\b", lowered) and re.search(
        r"\b(win|winner|race|elected|election)\b", lowered
    ):
        value = MarketType.SENATE_WINNER
    elif re.search(r"\b(house of representatives|congressional district|house race)\b", lowered):
        value = MarketType.HOUSE_WINNER
    elif re.search(r"\b(elected|election|electoral|race)\b", lowered):
        value = MarketType.ELECTION_WINNER
    else:
        return None
    return Evidence(value.value, EvidenceConfidence.HIGH, source)


def parse_features(
    title: str,
    *,
    reference_time: datetime | None = None,
    description: str = "",
    category: str | None = None,
) -> ParsedFeatures:
    normalized = normalize_title(title)
    direction_match = _DIRECTION_RE.search(title)
    direction = _DIRECTION_CANON.get(direction_match[1].lower()) if direction_match else None
    event_date = extract_event_date(title, reference_time)
    date_source = "title"
    if event_date is None and description:
        event_date = extract_event_date(description, reference_time)
        date_source = "description"
    event_year = extract_event_year(title, event_date)
    year_source = "title"
    if event_year is None and description:
        event_year = extract_event_year(description, event_date)
        year_source = "description"
    strike, strike_kind = _extract_strike_with_kind(title)
    strike_source = "title"
    if strike is None and description:
        strike, strike_kind = _extract_strike_with_kind(description)
        strike_source = "description"
    market_type = infer_market_type(title, category=category, source="title")
    if market_type is None and description:
        market_type = infer_market_type(description, category=category, source="description")
    date_text = title if date_source == "title" else description
    date_confidence = (
        EvidenceConfidence.HIGH if _YEAR_RE.search(date_text) else EvidenceConfidence.MEDIUM
    )
    return ParsedFeatures(
        normalized_title=normalized,
        strike=strike,
        direction=direction,
        event_date=event_date,
        event_year=event_year,
        market_type=MarketType(market_type.value) if market_type else None,
        strike_evidence=(
            Evidence(str(strike), EvidenceConfidence.HIGH, f"{strike_source}:{strike_kind}")
            if strike is not None
            else None
        ),
        event_date_evidence=(
            Evidence(
                event_date.isoformat(),
                date_confidence,
                date_source,
            )
            if event_date is not None
            else None
        ),
        event_year_evidence=(
            Evidence(str(event_year), EvidenceConfidence.MEDIUM, year_source)
            if event_year is not None
            else None
        ),
        market_type_evidence=market_type,
        tokens=frozenset(normalized.split()),
    )
