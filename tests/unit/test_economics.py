"""Economics engine tests, including SPEC Phase 4 reference scenarios recomputed
with the rates verified in docs/VERIFICATION.md.

Scenario math (verified rates):
1. Kalshi YES 0.90 / Poly NO 0.03, 4% cat, 100 sh:
   gross = 100×(1−0.90−0.03) = $7.00
   kalshi = ceil(0.07×100×0.90×0.10) = ceil(0.63) = $0.63
   poly   = 100×0.04×0.03×0.97 = 0.1164 → $0.11640
   net    = 7.00 − 0.63 − 0.1164 = $6.2536  (SPEC says ≈ $6.25 ✓)
2. Kalshi YES 0.61 / Poly NO 0.36, 3% sports, 1,000 sh:
   gross = 1000×0.03 = $30.00
   kalshi = ceil(0.07×1000×0.61×0.39) = ceil(16.653) = $16.66
   poly   = 1000×0.03×0.36×0.64 = 6.912 → $6.91200
   net    = 30 − 16.66 − 6.912 = $6.428  (SPEC says ≈ $6.43 ✓)
   1¢/share adverse execution = $10 > net → negative ✓
3. Kalshi YES 0.52 / Poly NO 0.46, 4% cat, 10,000 sh:
   gross = 10000×0.02 = $200.00
   kalshi = ceil(0.07×10000×0.52×0.48) = ceil(174.72) = $174.72
   poly   = 10000×0.04×0.46×0.54 = 99.36 → $99.36
   net    = 200 − 174.72 − 99.36 = −$74.08  (SPEC says ≈ −$74 ✓)
"""

from decimal import Decimal

from arb_scanner.app.books.depth import vwap_for_size
from arb_scanner.app.economics import Direction, evaluate_both_directions, evaluate_direction
from arb_scanner.app.fees.polymarket import FeeRateSource, FeeSchedule
from arb_scanner.app.fees.slippage import FixedCentsSlippage
from arb_scanner.app.types import BookLevel, Money, OrderBook, Side, Venue

D = Decimal

NO_SLIPPAGE = FixedCentsSlippage(cents_per_share=D(0))


def book(venue: Venue, side: Side, asks: list[tuple[str, int]]) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id="m",
        side=side,
        bids=(),
        asks=tuple(BookLevel(price=D(p), size=D(s)) for p, s in asks),
    )


def schedule(rate: str) -> FeeSchedule:
    return FeeSchedule(rate=D(rate), exponent=D(1), source=FeeRateSource.MARKET_METADATA)


class TestVwap:
    def test_single_level(self) -> None:
        result = vwap_for_size((BookLevel(price=D("0.5"), size=D(100)),), D(50))
        assert result.vwap == D("0.5") and not result.is_partial

    def test_multi_level_vwap(self) -> None:
        asks = (
            BookLevel(price=D("0.40"), size=D(100)),
            BookLevel(price=D("0.42"), size=D(200)),
        )
        result = vwap_for_size(asks, D(300))
        # (100×0.40 + 200×0.42)/300 = 124/300
        assert result.vwap == D("124") / D("300")
        assert result.levels_consumed == 2

    def test_partial_fill_flagged(self) -> None:
        result = vwap_for_size((BookLevel(price=D("0.5"), size=D(10)),), D(100))
        assert result.is_partial and result.fillable == D(10)

    def test_empty_book(self) -> None:
        result = vwap_for_size((), D(10))
        assert result.vwap is None and result.fillable == 0


class TestSanityScenarios:
    def test_scenario_1(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.90", 100)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.03", 100)]),
            size=100,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is not None
        assert evaluation.gross == Money.from_dollars("7.00")
        assert evaluation.fees.kalshi_fee == Money.from_dollars("0.63")
        assert evaluation.fees.polymarket_fee == Money.from_dollars("0.1164")
        assert evaluation.net == Money.from_dollars("6.2536")  # SPEC ≈ $6.25

    def test_scenario_2_sports(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.61", 1000)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.36", 1000)]),
            size=1000,
            poly_fee_schedule=schedule("0.03"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is not None
        assert evaluation.gross == Money.from_dollars("30.00")
        assert evaluation.fees.kalshi_fee == Money.from_dollars("16.66")
        assert evaluation.fees.polymarket_fee == Money.from_dollars("6.912")
        assert evaluation.net == Money.from_dollars("6.428")  # SPEC ≈ $6.43
        # ~1¢ total adverse execution kills it: break-even < 1¢/share
        assert evaluation.break_even_slippage_per_share < D("0.01")

    def test_scenario_2_goes_negative_with_one_cent_slippage(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.61", 1000)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.36", 1000)]),
            size=1000,
            poly_fee_schedule=schedule("0.03"),
            slippage_model=FixedCentsSlippage(cents_per_share=D(1)),
        )
        assert evaluation is not None
        assert evaluation.net < Money.zero()

    def test_scenario_3_negative_before_slippage(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.52", 10000)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.46", 10000)]),
            size=10000,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is not None
        assert evaluation.net == Money.from_dollars("-74.08")  # SPEC ≈ −$74


class TestDepthAdjustment:
    def test_walks_book_not_top_of_book(self) -> None:
        # second level is worse; VWAP must reflect it
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.90", 50), ("0.95", 50)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.03", 100)]),
            size=100,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is not None
        assert evaluation.kalshi_leg.vwap == D("0.925")
        assert evaluation.gross == Money.from_dollars("4.50")  # not 7.00

    def test_partial_depth_shrinks_size_and_flags(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, [("0.90", 40)]),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.03", 100)]),
            size=100,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is not None
        assert evaluation.size == 40
        assert evaluation.partial_fill_risk

    def test_no_depth_returns_none(self) -> None:
        evaluation = evaluate_direction(
            direction=Direction.KALSHI_YES_POLY_NO,
            kalshi_view=book(Venue.KALSHI, Side.YES, []),
            poly_book=book(Venue.POLYMARKET, Side.NO, [("0.03", 100)]),
            size=100,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert evaluation is None


class TestBothDirections:
    def test_two_directions_evaluated(self) -> None:
        results = evaluate_both_directions(
            kalshi_yes_view=book(Venue.KALSHI, Side.YES, [("0.90", 100)]),
            kalshi_no_view=book(Venue.KALSHI, Side.NO, [("0.12", 100)]),
            poly_yes_book=book(Venue.POLYMARKET, Side.YES, [("0.85", 100)]),
            poly_no_book=book(Venue.POLYMARKET, Side.NO, [("0.03", 100)]),
            size=100,
            poly_fee_schedule=schedule("0.04"),
            slippage_model=NO_SLIPPAGE,
        )
        assert {r.direction for r in results} == {
            Direction.KALSHI_YES_POLY_NO,
            Direction.KALSHI_NO_POLY_YES,
        }
        # direction 2: gross = 100×(1−0.12−0.85) = $3.00
        by_dir = {r.direction: r for r in results}
        assert by_dir[Direction.KALSHI_NO_POLY_YES].gross == Money.from_dollars("3.00")
