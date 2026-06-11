"""SQLAlchemy models. SQLite by default; Postgres via ARB_DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}  # noqa: RUF012


class MatchedPairRow(Base):
    """One Kalshi↔Polymarket pair with full matching provenance (SPEC Phase 3)."""

    __tablename__ = "matched_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kalshi_ticker: Mapped[str] = mapped_column(String(64), index=True)
    poly_condition_id: Mapped[str] = mapped_column(String(80), index=True)
    poly_yes_token_id: Mapped[str] = mapped_column(String(100))
    poly_no_token_id: Mapped[str] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), index=True)
    matched_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    differing_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rule_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BookSnapshotRow(Base):
    """Persisted book snapshot — the primary historical source for backtesting."""

    __tablename__ = "book_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(16), index=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class OpportunityRow(Base):
    """Every evaluated opportunity, alert or not, with its full evidence trail."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("matched_pairs.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(32))  # kalshi_yes_poly_no | kalshi_no_poly_yes
    size: Mapped[str] = mapped_column(String(32))  # Decimal as string
    gross_micros: Mapped[int] = mapped_column(Integer)
    net_micros: Mapped[int] = mapped_column(Integer)
    fee_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    book_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(16))  # alerted | rejected
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    realized_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
