"""Title normalization and structured-feature parsing (matching pipeline stages 1–2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

_NOISE_WORDS = frozenset(
    {"will", "the", "a", "an", "be", "by", "in", "on", "at", "to", "of", "this", "is"}
)

#: $70,000 / 70k / 3.5% / 70000
_STRIKE_RE = re.compile(r"\$?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(k)?%?", re.IGNORECASE)
_DIRECTION_RE = re.compile(r"\b(above|below|over|under|at least|at most|reach(?:es)?)\b", re.I)

_DIRECTION_CANON = {
    "above": "above",
    "over": "above",
    "at least": "above",
    "reach": "above",
    "reaches": "above",
    "below": "below",
    "under": "below",
    "at most": "below",
}


def normalize_title(title: str) -> str:
    """Stage 1: lowercase, strip punctuation/noise words, expand 70k → 70000."""
    text = title.lower()
    text = re.sub(r"\$(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m[1]) * 1000)), text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m[1]) * 1000)), text)
    text = text.replace(",", "")
    text = re.sub(r"[^\w\s.]", " ", text)
    text = re.sub(r"\.(?=\s|$)", "", text)  # trailing periods, keep decimals
    tokens = [t for t in text.split() if t not in _NOISE_WORDS]
    return " ".join(tokens)


@dataclass(frozen=True, slots=True)
class ParsedFeatures:
    """Stage 2 output: structured fields used for exact matching."""

    normalized_title: str
    strike: Decimal | None = None
    direction: str | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)


def parse_features(title: str) -> ParsedFeatures:
    normalized = normalize_title(title)
    strike: Decimal | None = None
    match = _STRIKE_RE.search(title.replace(",", ""))
    if match:
        value = Decimal(match[1].replace(",", ""))
        if match[2]:  # k suffix
            value *= 1000
        strike = value
    direction_match = _DIRECTION_RE.search(title)
    direction = _DIRECTION_CANON.get(direction_match[1].lower()) if direction_match else None
    return ParsedFeatures(
        normalized_title=normalized,
        strike=strike,
        direction=direction,
        tokens=frozenset(normalized.split()),
    )
