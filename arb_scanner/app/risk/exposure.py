"""Exposure tracking per venue and in total."""

from __future__ import annotations

from dataclasses import dataclass, field

from arb_scanner.app.types import Money, Venue


@dataclass
class ExposureTracker:
    _by_venue: dict[Venue, Money] = field(default_factory=dict)

    def add(self, venue: Venue, amount: Money) -> None:
        self._by_venue[venue] = self.venue_total(venue) + amount

    def release(self, venue: Venue, amount: Money) -> None:
        self._by_venue[venue] = self.venue_total(venue) - amount

    def venue_total(self, venue: Venue) -> Money:
        return self._by_venue.get(venue, Money.zero())

    def total(self) -> Money:
        result = Money.zero()
        for amount in self._by_venue.values():
            result = result + amount
        return result
