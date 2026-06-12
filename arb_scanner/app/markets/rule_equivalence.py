"""Rule-equivalence validation (matching pipeline stage 4 — MANDATORY) and the
final status decision (stage 5).

Two markets with identical titles are NOT the same bet unless their resolution
mechanics agree: source, determination time, void/DNP/50-50 handling, dispute
process. Known venue divergences encoded here (SPEC Phase 3):
- Polymarket resolution goes through UMA with a challenge window — always at
  least a warning, because outcomes can be disputed after apparent resolution.
- Sports: Polymarket auto-cancels sports limit orders at game start but may miss
  early starts; Kalshi may keep trading past close awaiting official confirmation.

Hard failures veto a match regardless of textual similarity. Warnings cap the
status at manual_review; nothing below threshold reaches the economics engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from arb_scanner.app.markets.parsers import US_STATE_NAMES

ACCEPT_THRESHOLD = 0.9
REVIEW_THRESHOLD = 0.6

# Settlement-basis divergence verified against live venue metadata on
# 2026-06-11 (docs/VERIFICATION.md §7): Kalshi GOVPARTY-family contracts pay
# on the party of the person SWORN IN/INAUGURATED (vacancy → first replacement
# sworn in; party locked to election day), while Polymarket governor markets
# pay on the called/certified ELECTION WINNER with party meaning the NOMINEE.
# Those bases settle differently in documented scenarios (governor-elect
# replaced before inauguration; party-registered candidate running as an
# independent), so pairs showing one basis on each venue are not equivalent.
_OFFICEHOLDER_BASIS = re.compile(
    r"\binaugurat\w*|\bsworn[\s-]+in\b|\bswearing[\s-]+in\b|"
    r"\bmember of the\b[^.]{0,60}\bparty\b",
    re.IGNORECASE,
)
_ELECTION_WINNER_BASIS = re.compile(
    r"\bwinner of the\b[^.]{0,80}\belection\b|\bnominee of the party\b|"
    r"\bcall(?:s|ed)? the race\b",
    re.IGNORECASE,
)


def settlement_basis_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect the verified Kalshi-officeholder vs Polymarket-election-winner split.

    Fires only when each venue's rules text shows exactly one of the two
    divergent bases: Kalshi sworn-in/inaugurated/member-of-party language and
    Polymarket winner/nominee/race-call language. Texts showing both bases (or
    neither) are ambiguous and are left to the existing conservative checks
    rather than rejected on this evidence.
    """
    if not _OFFICEHOLDER_BASIS.search(kalshi_text):
        return None
    if not _ELECTION_WINNER_BASIS.search(poly_text):
        return None
    if _OFFICEHOLDER_BASIS.search(poly_text) or _ELECTION_WINNER_BASIS.search(kalshi_text):
        return None
    return (
        "settlement_basis_conflict: Kalshi resolves on the officeholder sworn "
        "in/inaugurated while Polymarket resolves on the called/certified "
        "election winner (verified 2026-06-11, docs/VERIFICATION.md §7)"
    )


# Office-level divergence verified in the 2026-06-11 2,000-market dry-run
# (docs/VERIFICATION.md §8): Kalshi KXSTATELEG markets pay on STATE legislative
# chamber control ("wins the North Carolina State Senate … holding more
# seats"), while the title-similar Polymarket markets pay on the federal
# U.S. Senate race ("the 2026 midterm North Carolina U.S. Senate election").
# Different offices, different elections — never the same bet.
_STATE_LEGISLATURE_OFFICE = re.compile(
    r"\bstate\s+(?:senate|house|assembly|legislature)\b|"
    r"\bgeneral assembly\b|\blegislative chamber\b|\bholding more seats\b",
    re.IGNORECASE,
)
_FEDERAL_SENATE_OFFICE = re.compile(
    r"\b(?:u\.?\s?s\.?|united states|federal)\s+senate\b",
    re.IGNORECASE,
)


def _office_level(text: str) -> str | None:
    """Classify rules text as state-legislature or federal-senate, else None.

    Text matching both patterns is ambiguous and classifies as None: pairs
    without one clear office level on each side are left to the existing
    conservative checks (manual_review on missing facts), never rejected here.
    """
    state = bool(_STATE_LEGISLATURE_OFFICE.search(text))
    federal = bool(_FEDERAL_SENATE_OFFICE.search(text))
    if state and not federal:
        return "state_legislature"
    if federal and not state:
        return "federal_senate"
    return None


def office_level_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect a state-legislative-chamber market paired with a U.S. Senate market."""
    kalshi_level, poly_level = _office_level(kalshi_text), _office_level(poly_text)
    if kalshi_level is None or poly_level is None or kalshi_level == poly_level:
        return None
    return (
        "office_level_conflict: one venue resolves on state legislative "
        f"chamber control and the other on the U.S. Senate race "
        f"(kalshi={kalshi_level}, polymarket={poly_level}; "
        "verified 2026-06-11, docs/VERIFICATION.md §8)"
    )


# Basket/sweep divergence from the same dry-run: Kalshi
# KXDEMCOREFOURSENATESWEEP requires Democrats to win Senate races in Georgia,
# Michigan, North Carolina, AND Maine, while the paired Polymarket market is
# the single North Carolina race. An all-of-N-states contract is never
# equivalent to one of its legs.
_BASKET_PHRASES = re.compile(
    r"\ball of the following\b|\bsweep\b|\bwins? all\b|\bin all of\b",
    re.IGNORECASE,
)
_STATE_NAME_RE = re.compile(
    "|".join(
        rf"\b{name.replace(' ', r'\s+')}\b"
        for name in sorted(US_STATE_NAMES, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
# Two-or-more state names chained by commas/and/& ("Georgia, Michigan,
# North Carolina, AND Maine"). Candidate-name lists never match: only state
# names participate in the chain.
_STATE_CONJUNCTION_RE = re.compile(
    rf"(?:{_STATE_NAME_RE.pattern})"
    rf"(?:\s*(?:,|and\b|&)\s*(?:and\s+)?(?:{_STATE_NAME_RE.pattern}))+",
    re.IGNORECASE,
)


def _distinct_states(text: str) -> frozenset[str]:
    return frozenset(
        " ".join(match.group(0).lower().split()) for match in _STATE_NAME_RE.finditer(text)
    )


def _is_state_basket(text: str) -> bool:
    states = _distinct_states(text)
    if len(states) < 2:
        return False
    return bool(_BASKET_PHRASES.search(text)) or bool(_STATE_CONJUNCTION_RE.search(text))


def basket_scope_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect a multi-state all-must-win basket paired with a single-state race.

    Fires only when one side is a confident basket (>=2 distinct state names
    plus all-of/sweep wording or a comma/and chain of states) and the other
    side confidently references exactly one state. Both-basket pairs and
    zero-state (ambiguous) texts are left to the other checks.
    """
    kalshi_basket, poly_basket = _is_state_basket(kalshi_text), _is_state_basket(poly_text)
    if kalshi_basket == poly_basket:
        return None
    single_text = poly_text if kalshi_basket else kalshi_text
    if len(_distinct_states(single_text)) != 1:
        return None
    basket_venue = "kalshi" if kalshi_basket else "polymarket"
    single_venue = "polymarket" if kalshi_basket else "kalshi"
    return (
        f"basket_scope_conflict: {basket_venue} requires multiple states to "
        f"all resolve the same way while {single_venue} covers a single race "
        "(verified 2026-06-11, docs/VERIFICATION.md §8)"
    )


# Four conflict families verified in the 2026-06-11 5,000-market dry-run
# (docs/VERIFICATION.md §9). Each detector inspects title + rules text and
# fires only on one clear, opposing classification per side; anything
# ambiguous returns None and stays with the conservative checks.

_WORLD_CUP_RE = re.compile(r"\bworld cup\b", re.IGNORECASE)
_CONTINENT_RE = re.compile(
    r"\bsouth\s+america\b|\bnorth\s+america\b|\beurope\b|\bafrica\b|\basia\b|\boceania\b",
    re.IGNORECASE,
)
_CONTINENT_COMPLEMENT_RE = re.compile(
    r"\b(?:other than|not in|outside(?:\s+of)?)\s+"
    r"((?:south america|north america|europe|africa|asia|oceania)"
    r"(?:\s*(?:,|or|and)\s*(?:south america|north america|europe|africa|asia|oceania))*)",
    re.IGNORECASE,
)


def _continents_in(text: str) -> frozenset[str]:
    return frozenset(
        " ".join(match.group(0).lower().split()) for match in _CONTINENT_RE.finditer(text)
    )


_RESOLVES_NO_RE = re.compile(r"\bresolv\w*\s+(?:to\s+)?(?:[\"'“”]\s*)?no\b", re.IGNORECASE)


def _excluded_continents(text: str) -> frozenset[str]:
    """Continents a complement market excludes from its YES outcome.

    A complement phrase inside a resolves-to-No sentence is the inverse
    statement of an ordinary single-continent market ("if any country not in
    South America wins, the market resolves to No") and must not be read as
    the market's payout criterion.
    """
    for match in _CONTINENT_COMPLEMENT_RE.finditer(text):
        sentence_start = text.rfind(".", 0, match.start()) + 1
        sentence_end = text.find(".", match.end())
        sentence = text[sentence_start : sentence_end if sentence_end != -1 else len(text)]
        if _RESOLVES_NO_RE.search(sentence):
            continue
        return _continents_in(match.group(1))
    return frozenset()


def continent_scope_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect a not-X-or-Y continent complement paired with an excluded continent.

    Example: Kalshi "winner … from any continent other than Europe or South
    America" vs Polymarket "Will South America win the World Cup". Fires only
    when exactly one side is a complement and the other's title (the first
    line of the ``title\\nrules`` input) names exactly one continent inside
    the exclusion list. Only the title identifies the market's continent:
    multi-outcome rules text enumerates other continents as examples ("if
    France wins, the market will resolve to Europe"), so the rules text can
    never be used to classify the specific side.
    """
    if not (_WORLD_CUP_RE.search(kalshi_text) and _WORLD_CUP_RE.search(poly_text)):
        return None
    kalshi_excluded = _excluded_continents(kalshi_text)
    poly_excluded = _excluded_continents(poly_text)
    if bool(kalshi_excluded) == bool(poly_excluded):
        return None
    excluded = kalshi_excluded or poly_excluded
    specific_side = poly_text if kalshi_excluded else kalshi_text
    specific_title = specific_side.split("\n", 1)[0]
    named = _continents_in(specific_title)
    if len(named) != 1:
        return None
    (continent,) = named
    if continent not in excluded:
        return None
    return (
        "continent_scope_conflict: one venue resolves on the winner being from "
        f"any continent other than {', '.join(sorted(excluded))} while the "
        f"other resolves on {continent} winning "
        "(verified 2026-06-11, docs/VERIFICATION.md §9)"
    )


_STAGE_COUNT_TEXT_RE = re.compile(
    r"\bknockout (?:stage|round)\b|\bround of (?:16|32)\b", re.IGNORECASE
)
_TEAM_COUNT_TEXT_RE = re.compile(
    r"\b(?:at least|exactly|more than|fewer than)\b[^.\n]{0,20}\bteams?\b|\bteams? from\b",
    re.IGNORECASE,
)
_TOURNAMENT_WINNER_RE = re.compile(
    r"\bwin(?:s)?\b[^.\n]{0,80}\bworld cup\b|\bwinner of the\b[^.\n]{0,60}\bworld cup\b|"
    r"\bcontinent of the country that wins\b",
    re.IGNORECASE,
)


def sports_stage_vs_winner_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect a knockout-stage team-count market paired with a tournament winner."""

    def is_stage_count(text: str) -> bool:
        return bool(_STAGE_COUNT_TEXT_RE.search(text)) and bool(_TEAM_COUNT_TEXT_RE.search(text))

    kalshi_stage, poly_stage = is_stage_count(kalshi_text), is_stage_count(poly_text)
    if kalshi_stage == poly_stage:
        return None
    winner_side = poly_text if kalshi_stage else kalshi_text
    if _STAGE_COUNT_TEXT_RE.search(winner_side):
        return None  # ambiguous: stage language on the would-be winner side
    if not _TOURNAMENT_WINNER_RE.search(winner_side):
        return None
    return (
        "sports_stage_vs_winner_conflict: one venue counts teams reaching the "
        "knockout stage while the other resolves on the tournament winner "
        "(verified 2026-06-11, docs/VERIFICATION.md §9)"
    )


_CRYPTO_ASSET_RE = re.compile(r"\b(?:bitcoin|btc|ethereum|eth|solana|sol)\b", re.IGNORECASE)
_CRYPTO_BEST_MONTH_RE = re.compile(
    r"\bbest(?:[- ]performing)? month\b|\bhighest percentage change\b|"
    r"\bmonthly candle\b|\bmonthly performance\b|\bworst month\b",
    re.IGNORECASE,
)
_PRICE_THRESHOLD_TEXT_RE = re.compile(
    r"\b(?:above|below|exceed(?:s|ed)?|at or above|at or below)\b[^.\n]{0,40}\$?\d[\d,]{2,}",
    re.IGNORECASE,
)


def crypto_performance_vs_price_threshold_conflict(
    kalshi_text: str, poly_text: str
) -> str | None:
    """Detect a crypto price-threshold market paired with a best-month market.

    A month name alone never fires: the performance side must show explicit
    best-month/percentage-change/monthly-candle language, and the other side
    an explicit price threshold.
    """
    if not (_CRYPTO_ASSET_RE.search(kalshi_text) and _CRYPTO_ASSET_RE.search(poly_text)):
        return None
    kalshi_perf = bool(_CRYPTO_BEST_MONTH_RE.search(kalshi_text))
    poly_perf = bool(_CRYPTO_BEST_MONTH_RE.search(poly_text))
    if kalshi_perf == poly_perf:
        return None
    threshold_side = poly_text if kalshi_perf else kalshi_text
    if not _PRICE_THRESHOLD_TEXT_RE.search(threshold_side):
        return None
    return (
        "crypto_performance_vs_price_threshold_conflict: one venue resolves on "
        "relative monthly performance while the other resolves on a fixed "
        "price threshold (verified 2026-06-11, docs/VERIFICATION.md §9)"
    )


_STOCK_INDEX_TEXT_RE = re.compile(
    r"\b(?:s\s*&\s*p\s*500|spx|nasdaq(?:[- ]?100)?|ndx|dow(?:\s+jones)?|djia|"
    r"russell\s*2000)\b",
    re.IGNORECASE,
)
_INTRAMONTH_HIGH_RE = re.compile(
    r"\bhit\b[^.\n]{0,40}\(?\s*high\s*\)?|\bat any point\b|"
    r"\bany 1[- ]minute candle\b|\bintra[- ]?month high\b",
    re.IGNORECASE,
)
_FIXED_CLOSE_RE = re.compile(
    r"\bclos(?:e|es|ing)\b|\bfinal trading day\b|\bfinal day of trading\b|"
    r"\bend of (?:the )?day\b|\beod\b|\bindex value on\b|\bat \d{1,2}(?::\d{2})?\s?[ap]m\b",
    re.IGNORECASE,
)


def stock_close_vs_intramonth_high_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect a fixed date/close index threshold paired with an intramonth high.

    Intramonth-high language takes priority when classifying a side ("at any
    point … market close on the final day" is a high market, not a close
    market), so close-vs-close and high-vs-high pairs always fall through.
    """
    if not (_STOCK_INDEX_TEXT_RE.search(kalshi_text) and _STOCK_INDEX_TEXT_RE.search(poly_text)):
        return None

    def classify(text: str) -> str | None:
        if _INTRAMONTH_HIGH_RE.search(text):
            return "intramonth_high"
        if _FIXED_CLOSE_RE.search(text):
            return "fixed_close"
        return None

    kalshi_kind, poly_kind = classify(kalshi_text), classify(poly_text)
    if kalshi_kind is None or poly_kind is None or kalshi_kind == poly_kind:
        return None
    return (
        "stock_close_vs_intramonth_high_conflict: one venue resolves on a "
        "fixed date/time index value while the other resolves on the index "
        "trading through a level at any point in the month "
        "(verified 2026-06-11, docs/VERIFICATION.md §9)"
    )


# Cancellation/void-policy basis extraction, verified 2026-06-11 against
# KXWCCONTINENT-26-SA vs Polymarket "South America wins the 2026 FIFA World
# Cup" (docs/VERIFICATION.md §10). Kalshi ACHIEVEMENTS-style contracts settle
# a cancelled event at FAIR VALUE (last traded price / Outcome Review
# Committee / $1-over-N split); Polymarket negRisk events resolve to "Other",
# paying each named outcome a hard No. Those bases pay differently in the
# cancellation state, so a pair proven to hold one basis on each venue is not
# arbitrage-equivalent even when normal-state outcomes coincide.
_CANCELLATION_POLICY_TERMS: tuple[tuple[str, str], ...] = (
    ("fair_value", r"\bfair[- ]value\b|\blast traded price\b"),
    ("committee_review", r"\b(?:outcome )?review committee\b"),
    ("split_or_1_over_n", r"\$1\s*/\s*\[|\bsplit equally\b|\bequal(?:ly)? split\b"),
    ("resolves_to_other", r"\bresolve[sd]?\s+to\s+[“”\"']?other\b"),
    ("hard_no_on_other", r"\bno winner\b[^.\n]{0,60}\b(?:declared|timeframe)\b"),
    ("cancellation", r"\bcancel(?:led|ed|lation)?\b"),
    ("postponement_deadline", r"\bpostpon\w*\s+(?:after|past|beyond)\b[^.\n]{0,40}\d{4}"),
)
_FAIR_VALUE_FAMILY = frozenset({"fair_value", "committee_review", "split_or_1_over_n"})
_RESOLVES_OTHER_FAMILY = frozenset({"resolves_to_other", "hard_no_on_other"})


def cancellation_policy_terms(text: str) -> tuple[str, ...]:
    """Named cancellation-handling terms present in rules text."""
    return tuple(
        name
        for name, pattern in _CANCELLATION_POLICY_TERMS
        if re.search(pattern, text, re.IGNORECASE)
    )


def cancellation_policy_basis(text: str) -> str | None:
    """Classify rules text as fair_value_settlement or resolves_to_other.

    Returns None when neither family is present or when both are (ambiguous
    text never proves a basis).
    """
    terms = frozenset(cancellation_policy_terms(text))
    fair_value = bool(terms & _FAIR_VALUE_FAMILY)
    other = bool(terms & _RESOLVES_OTHER_FAMILY)
    if fair_value and not other:
        return "fair_value_settlement"
    if other and not fair_value:
        return "resolves_to_other"
    return None


# Source/finalization basis extraction, verified 2026-06-11 against
# KXINXDIRY-26DEC31H1600-T8000 vs Polymarket "close over $8,000 on the final
# trading day of December 2026" (docs/VERIFICATION.md §11). Kalshi's
# underlying is a fixed-time index snapshot documented by Kalshi itself with
# post-expiration revisions ignored; Polymarket resolves on the official
# (Yahoo-published historical) closing price finalized through UMA. Those
# usually agree but can diverge near a strike on corrections, auction delays,
# or halts — a diagnostic-only mismatch, never a hard rejection.
_SOURCE_FINALIZATION_TERMS: tuple[tuple[str, str], ...] = (
    ("fixed_time_snapshot", r"\bindex value on\b[^.\n]{0,80}\bat \d{1,2}(?::\d{2})?\s?[ap]m\b"),
    ("revisions_ignored_after_expiration", r"\brevisions?\b[^.\n]{0,80}\bnot be accounted\b"),
    ("source_agency_kalshi", r"\bsource agency is kalshi\b"),
    ("no_data_extension", r"\bmost recently available prior\b"),
    ("market_outcome_review", r"\bmarket outcome review\b"),
    ("official_close", r"\bofficial closing price\b"),
    ("historical_close", r"\bhistorical prices\b|\bclose[\"”']? prices\b"),
    ("yahoo_finance_close", r"\byahoo finance\b"),
    ("uma_finalization", r"\buma\b"),
    ("last_valid_trade", r"\blast valid on-exchange trade\b"),
)
_SNAPSHOT_BASIS_FAMILY = frozenset(
    {
        "fixed_time_snapshot",
        "revisions_ignored_after_expiration",
        "source_agency_kalshi",
        "no_data_extension",
    }
)
_OFFICIAL_CLOSE_FAMILY = frozenset(
    {"official_close", "historical_close", "yahoo_finance_close", "last_valid_trade"}
)


def source_finalization_terms(text: str) -> tuple[str, ...]:
    """Named source/finalization-mechanics terms present in rules text."""
    return tuple(
        name
        for name, pattern in _SOURCE_FINALIZATION_TERMS
        if re.search(pattern, text, re.IGNORECASE)
    )


def source_finalization_basis(text: str) -> str | None:
    """Classify rules text as fixed_time_snapshot or official_close basis.

    Returns None when neither family is present or both are: ambiguous text
    never proves a basis. Wording-only differences (neither family matched)
    classify as None and produce no diagnostic.
    """
    terms = frozenset(source_finalization_terms(text))
    snapshot = bool(terms & _SNAPSHOT_BASIS_FAMILY)
    official = bool(terms & _OFFICIAL_CLOSE_FAMILY)
    if snapshot and not official:
        return "fixed_time_snapshot"
    if official and not snapshot:
        return "official_close"
    return None


# Candidate-slate extraction, verified 2026-06-11 against
# KXDEMPROGRESSIVESENATESWEEP-26NOV03 vs Polymarket "Will Democratic Senate
# incumbents win all their nominating elections in the 2026 cycle?"
# (docs/VERIFICATION.md §12). Kalshi enumerates a FIXED NAMED SLATE
# ("Juliana Stratton in Illinois, … Mallory McMorrow OR Abdul El-Sayed in
# Michigan, …"), mostly challengers; Polymarket's set is the INCUMBENT COHORT,
# membership conditioned on registering for reelection. A fixed slate can
# never equal a registration-dependent cohort, and a slate member's loss
# flips one venue without touching the other.
_PERSON_NAME = r"[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){1,2}"
_CANDIDATE_GROUP_RE = re.compile(
    rf"({_PERSON_NAME})(?:\s+(?:OR|or)\s+({_PERSON_NAME}))?\s+in\s+"
    r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"
)
_ALL_OF_SWEEP_RE = re.compile(
    r"\ball\b[^.\n]{0,60}\bwin\b|\bwin(?:s)?\s+all\b|\ball of the following\b",
    re.IGNORECASE,
)
_INCUMBENT_COHORT_RE = re.compile(
    r"\bincumbents?\b[^.\n]{0,120}\b(?:nominat\w+|primar\w+)\b|"
    r"\b(?:nominat\w+|primar\w+)\b[^.\n]{0,120}\bincumbents?\b",
    re.IGNORECASE,
)


def extract_candidate_slate(text: str) -> frozenset[frozenset[str]]:
    """Named candidate groups from a fixed-slate sweep market.

    Each group is a set of interchangeable alternatives ("Mallory McMorrow OR
    Abdul El-Sayed" is one group). Names are normalized to lowercase. Only the
    "Name in State" enumeration shape is parsed; anything else extracts
    nothing rather than guessing.
    """
    groups: list[frozenset[str]] = []
    for match in _CANDIDATE_GROUP_RE.finditer(text):
        alternatives = frozenset(
            " ".join(name.lower().split())
            for name in (match.group(1), match.group(2))
            if name
        )
        groups.append(alternatives)
    return frozenset(groups)


def candidate_set_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect provably different all-of candidate sets on the two venues.

    Fires when both sides are all-of sweep markets and either (a) both
    enumerate named slates that differ, or (b) one enumerates a fixed named
    slate (>= 2 groups) while the other defines its set as the incumbent
    cohort without naming candidates. One-sided extraction without cohort
    evidence never fires — callers surface that as candidate_set_mismatch.
    """
    if not (_ALL_OF_SWEEP_RE.search(kalshi_text) and _ALL_OF_SWEEP_RE.search(poly_text)):
        return None
    kalshi_slate = extract_candidate_slate(kalshi_text)
    poly_slate = extract_candidate_slate(poly_text)
    if len(kalshi_slate) >= 2 and len(poly_slate) >= 2:
        if kalshi_slate != poly_slate:
            return (
                "candidate_set_conflict: the venues enumerate different "
                "candidate slates for their all-of sweep "
                "(verified 2026-06-11, docs/VERIFICATION.md §12)"
            )
        return None
    for slate, other_text in ((kalshi_slate, poly_text), (poly_slate, kalshi_text)):
        if (
            len(slate) >= 2
            and not extract_candidate_slate(other_text)
            and _INCUMBENT_COHORT_RE.search(other_text)
        ):
            return (
                "candidate_set_conflict: one venue requires a fixed named "
                "candidate slate to sweep while the other tracks the "
                "registration-dependent incumbent cohort "
                "(verified 2026-06-11, docs/VERIFICATION.md §12)"
            )
    return None


def void_policy_conflict(kalshi_text: str, poly_text: str) -> str | None:
    """Detect provably incompatible cancellation settlement bases.

    Fires only when BOTH sides' rules text proves a basis and the bases
    differ. One-sided or ambiguous extraction never fires — callers surface
    that as a void_policy_mismatch warning instead, keeping the pair in
    manual_review rather than rejecting on unproven evidence.
    """
    kalshi_basis = cancellation_policy_basis(kalshi_text)
    poly_basis = cancellation_policy_basis(poly_text)
    if kalshi_basis is None or poly_basis is None or kalshi_basis == poly_basis:
        return None
    return (
        "void_policy_conflict: incompatible cancellation settlement "
        f"(kalshi={kalshi_basis}, polymarket={poly_basis}); a cancelled event "
        "pays fair value on one venue and a hard No on the other "
        "(verified 2026-06-11, docs/VERIFICATION.md §10)"
    )


class MatchStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class KalshiRuleFacts:
    determination_time: datetime | None
    resolution_source: str
    resolution_text: str
    can_close_early: bool
    is_sports: bool
    void_policy: str | None
    sports_policy: tuple[str, ...] = ()
    # Market title: some structured conflicts (continent scope, stage count,
    # crypto month, intramonth high) are only visible in the question text.
    title: str = ""


@dataclass(frozen=True, slots=True)
class PolymarketRuleFacts:
    determination_time: datetime | None
    resolution_source: str
    resolution_text: str
    uma_resolution: bool
    is_sports: bool
    game_start_time: datetime | None
    void_policy: str | None
    sports_policy: tuple[str, ...] = ()
    title: str = ""


@dataclass(frozen=True, slots=True)
class RuleEquivalenceResult:
    hard_failures: tuple[str, ...]
    warnings: tuple[str, ...]
    missing_fields: tuple[str, ...]


def validate_rules(kalshi: KalshiRuleFacts, poly: PolymarketRuleFacts) -> RuleEquivalenceResult:
    failures: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    # Determination time: end date vs determination time must agree exactly.
    if kalshi.determination_time and poly.determination_time:
        if kalshi.determination_time != poly.determination_time:
            failures.append(
                "determination time differs: "
                f"kalshi={kalshi.determination_time} poly={poly.determination_time}"
            )
    else:
        warnings.append("determination time unverified on at least one venue")
        missing.append("determination_time")

    # Resolution source.
    k_src, p_src = kalshi.resolution_source.strip().lower(), poly.resolution_source.strip().lower()
    if k_src and p_src:
        if k_src != p_src:
            failures.append(f"resolution source differs: {k_src!r} vs {p_src!r}")
    else:
        warnings.append("resolution source unverified on at least one venue")
        missing.append("resolution_source")

    if not kalshi.resolution_text.strip() or not poly.resolution_text.strip():
        warnings.append("market resolution text missing on at least one venue")
        missing.append("resolution_text")

    # Structured text-evidence conflicts verified against live venue rules
    # (docs/VERIFICATION.md §7–9) — each detection is a hard failure. These
    # only ever reject; ambiguous text falls through to the warnings above.
    for detect in (settlement_basis_conflict, office_level_conflict, basket_scope_conflict):
        conflict = detect(kalshi.resolution_text, poly.resolution_text)
        if conflict:
            failures.append(conflict)
    # These four read titles too: the distinguishing language (continent
    # complement, team counts, best-month, intramonth high) often appears
    # only in the question text. Convention: the first line of the combined
    # text is the market title, so an empty title must not inject a blank
    # first line.
    kalshi_combined = (
        f"{kalshi.title}\n{kalshi.resolution_text}" if kalshi.title else kalshi.resolution_text
    )
    poly_combined = (
        f"{poly.title}\n{poly.resolution_text}" if poly.title else poly.resolution_text
    )
    for detect in (
        continent_scope_conflict,
        sports_stage_vs_winner_conflict,
        crypto_performance_vs_price_threshold_conflict,
        stock_close_vs_intramonth_high_conflict,
        void_policy_conflict,
        candidate_set_conflict,
    ):
        conflict = detect(kalshi_combined, poly_combined)
        if conflict:
            failures.append(conflict)
    # One side enumerates a named slate but the other side's set is neither a
    # slate nor a provable cohort: can't prove a conflict, but the sets are
    # unverified — surface explicitly and stay in manual_review.
    kalshi_slate = extract_candidate_slate(kalshi_combined)
    poly_slate = extract_candidate_slate(poly_combined)
    if (
        not any("candidate_set_conflict" in failure for failure in failures)
        and (len(kalshi_slate) >= 2) != (len(poly_slate) >= 2)
        and _ALL_OF_SWEEP_RE.search(kalshi_combined)
        and _ALL_OF_SWEEP_RE.search(poly_combined)
    ):
        warnings.append(
            "candidate_set_mismatch: one venue enumerates a named candidate "
            "slate but the other side's candidate set could not be extracted"
        )
        missing.append("candidate_set")
    # One-sided basis extraction can't prove a conflict (e.g. Kalshi's fair-
    # value handling lives in series-level contract terms the scanner never
    # fetches), but it is a stronger signal than "void policy unknown":
    # surface it explicitly and keep the pair in manual_review.
    kalshi_basis = cancellation_policy_basis(kalshi_combined)
    poly_basis = cancellation_policy_basis(poly_combined)
    if (kalshi_basis is None) != (poly_basis is None):
        warnings.append(
            f"void_policy_mismatch: kalshi={kalshi_basis or 'unknown'} "
            f"polymarket={poly_basis or 'unknown'} — cancellation handling "
            "unverified on one venue"
        )
        missing.append("void_policy_basis")

    # Source/finalization mechanics: a fixed-time snapshot underlying vs an
    # official/historical close underlying can diverge near a strike on
    # corrections or halts. Diagnostic only — both bases must be proven and
    # different; same-basis or ambiguous pairs get no warning. This warning
    # keeps the pair in manual_review and can never accept (warnings only
    # tighten decide_status).
    kalshi_source_basis = source_finalization_basis(kalshi_combined)
    poly_source_basis = source_finalization_basis(poly_combined)
    if (
        kalshi_source_basis is not None
        and poly_source_basis is not None
        and kalshi_source_basis != poly_source_basis
    ):
        warnings.append(
            f"source_finalization_mismatch: kalshi={kalshi_source_basis} "
            f"polymarket={poly_source_basis} — underlying value is finalized "
            "differently on each venue"
        )
        missing.append("source_finalization_basis")

    # Void / DNP / postponement handling.
    if kalshi.void_policy is None or poly.void_policy is None:
        warnings.append("void policy unknown on at least one venue")
        missing.append("void_policy")
    elif kalshi.void_policy != poly.void_policy:
        failures.append(f"void policy differs: kalshi={kalshi.void_policy} poly={poly.void_policy}")

    # UMA challenge window applies to Polymarket-resolved markets.
    if poly.uma_resolution:
        warnings.append("UMA challenge window: Polymarket outcome can be disputed post-resolution")

    # Sports early-start divergence.
    if kalshi.is_sports or poly.is_sports:
        if kalshi.sports_policy and poly.sports_policy:
            if kalshi.sports_policy != poly.sports_policy:
                failures.append(
                    "sports postponement/cancellation policy differs: "
                    f"kalshi={kalshi.sports_policy} poly={poly.sports_policy}"
                )
        elif kalshi.sports_policy or poly.sports_policy:
            warnings.append("sports postponement/cancellation policy unverified")
            missing.append("sports_postponement_policy")
        if poly.game_start_time is not None or kalshi.can_close_early:
            warnings.append(
                "sports early start risk: Polymarket cancels limit orders at "
                "scheduled start (may miss early starts); Kalshi may trade past close"
            )

    return RuleEquivalenceResult(
        hard_failures=tuple(failures),
        warnings=tuple(warnings),
        missing_fields=tuple(dict.fromkeys(missing)),
    )


def decide_status(
    similarity_score: float,
    rules: RuleEquivalenceResult,
    *,
    accept_threshold: float = ACCEPT_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
) -> MatchStatus:
    """Stage 5. Hard rule failures veto; warnings cap at manual_review."""
    if rules.hard_failures:
        return MatchStatus.REJECTED
    if similarity_score < review_threshold:
        return MatchStatus.REJECTED
    # More than the ever-present UMA warning means a human must look.
    substantive_warnings = len(rules.warnings)
    if similarity_score >= accept_threshold and substantive_warnings <= 1:
        return MatchStatus.ACCEPTED
    return MatchStatus.MANUAL_REVIEW
