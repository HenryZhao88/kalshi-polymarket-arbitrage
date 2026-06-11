"""Repository layer over the SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from arb_scanner.app.economics import OpportunityEvaluation
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
            matched_fields=pair.persisted_fields(),
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

    async def list_recent(
        self, *, status: str | None = None, limit: int = 20
    ) -> list[MatchedPairRow]:
        statement = select(MatchedPairRow)
        if status is not None:
            statement = statement.where(MatchedPairRow.status == status)
        result = await self._session.execute(
            statement.order_by(desc(MatchedPairRow.created_at), desc(MatchedPairRow.id)).limit(
                limit
            )
        )
        return list(result.scalars())

    async def list_latest_scan(
        self, *, limit: int = 20, status: str | None = None
    ) -> list[MatchedPairRow]:
        result = await self._session.execute(
            select(MatchedPairRow)
            .order_by(desc(MatchedPairRow.created_at), desc(MatchedPairRow.id))
            .limit(max(limit * 20, 1000))
        )
        rows = list(result.scalars())
        if not rows:
            return []
        scan_id = rows[0].matched_fields.get("scan_id")
        if not scan_id:
            candidates = rows
        else:
            candidates = [row for row in rows if row.matched_fields.get("scan_id") == scan_id]
        if status is not None:
            candidates = [row for row in candidates if row.status == status]
        return candidates[:limit]


class SnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, venue: str, market_id: str, payload: dict[str, Any]) -> BookSnapshotRow:
        row = BookSnapshotRow(venue=venue, market_id=market_id, payload=payload)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_all(self) -> list[BookSnapshotRow]:
        result = await self._session.execute(
            select(BookSnapshotRow).order_by(BookSnapshotRow.captured_at)
        )
        return list(result.scalars())


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


def fee_breakdown_payload(evaluation: OpportunityEvaluation) -> dict[str, str]:
    fees = evaluation.fees
    return {
        "kalshi_fee": str(fees.kalshi_fee.to_dollars()),
        "polymarket_fee": str(fees.polymarket_fee.to_dollars()),
        "bridge_cost": str(fees.bridge_cost.to_dollars()),
        "withdrawal_cost": str(fees.withdrawal_cost.to_dollars()),
        "gas_cost": str(fees.gas_cost.to_dollars()),
        "processor_cost": str(fees.processor_cost.to_dollars()),
        "conversion_cost": str(fees.conversion_cost.to_dollars()),
        "slippage_cost": str(fees.slippage_cost.to_dollars()),
        "unknown_cost_buffer": str(fees.unknown_cost_buffer.to_dollars()),
        "latency_miss": str(fees.latency_miss.to_dollars()),
    }


class SqlAlchemyScanStore:
    """Persistence adapter used by live/dry-run scanning."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        persist_raw_candidates: bool = False,
        max_candidates_per_scan: int = 5000,
    ) -> None:
        self._session = session
        self._persist_raw_candidates = persist_raw_candidates
        self._max_candidates_per_scan = max_candidates_per_scan
        self._persisted_candidates = 0

    async def add_pair(self, pair: MatchedPair) -> int | None:
        raw_rejection = pair.status.value == "rejected" and pair.status_reasons == (
            "similarity below structured-review threshold",
        )
        if raw_rejection and not self._persist_raw_candidates:
            return None
        if (
            pair.status.value == "rejected"
            and self._persisted_candidates >= self._max_candidates_per_scan
        ):
            return None
        row = await PairRepo(self._session).add(pair)
        self._persisted_candidates += 1
        assert row.id is not None
        return row.id

    async def apply_retention(self, cutoff: datetime) -> None:
        await self._session.execute(
            delete(OpportunityRow).where(OpportunityRow.created_at < cutoff)
        )
        await self._session.execute(
            delete(BookSnapshotRow).where(BookSnapshotRow.captured_at < cutoff)
        )
        await self._session.execute(
            delete(MatchedPairRow).where(MatchedPairRow.created_at < cutoff)
        )

    async def add_snapshot(self, venue: str, market_id: str, payload: dict[str, Any]) -> int:
        row = await SnapshotRepo(self._session).add(venue, market_id, payload)
        assert row.id is not None
        return row.id

    async def add_opportunity(
        self,
        *,
        pair_id: int,
        evaluation: OpportunityEvaluation,
        decision: str,
        rejection_reason: str | None,
        assumptions: dict[str, Any],
        paired_snapshot: dict[str, Any],
    ) -> int:
        row = await OpportunityRepo(self._session).add(
            pair_id=pair_id,
            direction=evaluation.direction.value,
            size=str(evaluation.executable_size),
            gross_micros=evaluation.gross.micros,
            net_micros=evaluation.net.micros,
            fee_breakdown=fee_breakdown_payload(evaluation),
            assumptions=assumptions,
            book_snapshot=paired_snapshot,
            decision=decision,
            rejection_reason=rejection_reason,
        )
        assert row.id is not None
        return row.id
