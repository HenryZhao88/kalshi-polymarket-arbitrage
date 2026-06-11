"""Repository layer over the SQLAlchemy models."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arb_scanner.app.markets.discovery import MatchedPair
from arb_scanner.app.storage.models import BookSnapshotRow, MatchedPairRow, OpportunityRow


class PairRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, pair: MatchedPair) -> MatchedPairRow:
        row = MatchedPairRow(
            kalshi_ticker=pair.kalshi_ticker,
            poly_condition_id=pair.poly_condition_id,
            poly_yes_token_id=pair.poly_yes_token_id,
            poly_no_token_id=pair.poly_no_token_id,
            confidence=pair.confidence,
            status=pair.status.value,
            matched_fields=dict(pair.matched_fields),
            differing_fields=dict(pair.differing_fields),
            rule_warnings=list(pair.rule_warnings),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_by_status(self, status: str) -> list[MatchedPairRow]:
        result = await self._session.execute(
            select(MatchedPairRow).where(MatchedPairRow.status == status)
        )
        return list(result.scalars())


class SnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, venue: str, market_id: str, payload: dict[str, Any]) -> BookSnapshotRow:
        row = BookSnapshotRow(venue=venue, market_id=market_id, payload=payload)
        self._session.add(row)
        await self._session.flush()
        return row


class OpportunityRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, **fields: Any) -> OpportunityRow:
        row = OpportunityRow(**fields)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_all(self) -> list[OpportunityRow]:
        result = await self._session.execute(
            select(OpportunityRow).order_by(OpportunityRow.created_at)
        )
        return list(result.scalars())
