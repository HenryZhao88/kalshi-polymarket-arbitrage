"""Storage round-trip tests against in-memory SQLite."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arb_scanner.app.markets.discovery import MatchedPair, evaluate_pair
from arb_scanner.app.markets.rule_equivalence import MatchStatus
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.repo import OpportunityRepo, PairRepo, SnapshotRepo


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    factory = make_session_factory(engine)
    async with factory() as sess:
        yield sess
    await engine.dispose()


PAIR = MatchedPair(
    kalshi_ticker="KXBTCD-26JUN30-T70000",
    poly_condition_id="0xabc",
    poly_yes_token_id="111",
    poly_no_token_id="222",
    confidence=0.93,
    status=MatchStatus.ACCEPTED,
    matched_fields={"similarity_stage": "structured"},
    differing_fields={},
    rule_warnings=("UMA challenge window",),
)


class TestPairRepo:
    async def test_round_trip(self, session: AsyncSession) -> None:
        repo = PairRepo(session)
        row = await repo.add(PAIR)
        assert row.id is not None
        accepted = await repo.list_by_status("accepted")
        assert len(accepted) == 1
        assert accepted[0].kalshi_ticker == PAIR.kalshi_ticker
        assert accepted[0].rule_warnings == ["UMA challenge window"]

    async def test_filtered_by_status(self, session: AsyncSession) -> None:
        repo = PairRepo(session)
        await repo.add(PAIR)
        assert await repo.list_by_status("rejected") == []


class TestSnapshotAndOpportunity:
    async def test_snapshot_round_trip(self, session: AsyncSession) -> None:
        snap = await SnapshotRepo(session).add("kalshi", "TICKER", {"yes_dollars": []})
        assert snap.id is not None and snap.captured_at is not None

    async def test_opportunity_round_trip(self, session: AsyncSession) -> None:
        repo = OpportunityRepo(session)
        await repo.add(
            pair_id=None,
            direction="kalshi_yes_poly_no",
            size="100",
            gross_micros=7_000_000,
            net_micros=6_253_600,
            fee_breakdown={"kalshi_fee": "0.63"},
            decision="alerted",
        )
        rows = await repo.list_all()
        assert rows[0].net_micros == 6_253_600


class TestEvaluatePairEndToEnd:
    def test_btc_strike_market_match(self) -> None:
        kalshi_market = {
            "ticker": "KXBTCD-26JUN30-T70000",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
            "category": "crypto",
            "rules_primary": "coindesk index",
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Will BTC be above $70k on June 30?",
            "endDate": "2026-06-30T16:00:00Z",
            "resolutionSource": "coindesk index",
            "clobTokenIds": '["111", "222"]',
            "tags": ["Crypto"],
        }
        pair = evaluate_pair(kalshi_market, poly_market)
        assert pair is not None
        assert pair.status in (MatchStatus.ACCEPTED, MatchStatus.MANUAL_REVIEW)
        assert pair.confidence >= 0.7
        assert pair.poly_yes_token_id == "111"

    def test_trap_different_end_dates_rejected(self) -> None:
        kalshi_market = {
            "ticker": "K1",
            "title": "Bitcoin above $70,000 on June 30?",
            "expected_expiration_time": "2026-06-30T16:00:00Z",
            "can_close_early": False,
        }
        poly_market = {
            "conditionId": "0xabc",
            "question": "Bitcoin above $70,000 on June 30?",
            "endDate": "2026-07-01T16:00:00Z",  # same title, different determination
            "clobTokenIds": '["111", "222"]',
        }
        pair = evaluate_pair(kalshi_market, poly_market)
        assert pair is not None
        assert pair.status is MatchStatus.REJECTED
        assert any("determination" in v for v in pair.differing_fields.values())

    def test_market_without_tokens_skipped(self) -> None:
        assert evaluate_pair({"ticker": "K"}, {"question": "x"}) is None
