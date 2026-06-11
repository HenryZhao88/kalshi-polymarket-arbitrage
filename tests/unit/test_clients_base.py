"""Rate limiter, circuit breaker, and error-mapping unit tests."""

import asyncio
import time

import pytest

from arb_scanner.app.clients.base import (
    AuthError,
    BadRequestError,
    CircuitBreaker,
    CircuitOpenError,
    NotFoundError,
    RateLimitedError,
    ServerError,
    SlidingWindowLimiter,
    TokenBucket,
    TooEarlyError,
    error_for_status,
)


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "exc"),
        [
            (400, BadRequestError),
            (401, AuthError),
            (403, AuthError),
            (404, NotFoundError),
            (425, TooEarlyError),
            (429, RateLimitedError),
            (500, ServerError),
            (503, ServerError),
        ],
    )
    def test_status_maps(self, status: int, exc: type[Exception]) -> None:
        assert isinstance(error_for_status(status, "x"), exc)


class TestTokenBucket:
    async def test_burst_until_empty_then_waits(self) -> None:
        # 200 tokens/s, capacity 200, cost 10 → 20 immediate requests
        bucket = TokenBucket(refill_rate=200, capacity=200)
        start = time.monotonic()
        for _ in range(20):
            await bucket.acquire(10)
        assert time.monotonic() - start < 0.05  # burst is instant
        await bucket.acquire(10)  # 21st must wait ≈ 50ms for refill
        assert time.monotonic() - start >= 0.04

    async def test_refill_caps_at_capacity(self) -> None:
        bucket = TokenBucket(refill_rate=1000, capacity=10)
        await bucket.acquire(10)
        await asyncio.sleep(0.05)  # would refill 50 without the cap
        start = time.monotonic()
        await bucket.acquire(10)
        await bucket.acquire(10)  # second must wait: cap prevented banking
        assert time.monotonic() - start >= 0.005


class TestSlidingWindow:
    async def test_blocks_after_window_full(self) -> None:
        limiter = SlidingWindowLimiter(max_requests=3, window_seconds=0.1)
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        assert time.monotonic() - start < 0.05
        await limiter.acquire()  # must wait for the first stamp to expire
        assert time.monotonic() - start >= 0.09


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)
        for _ in range(3):
            breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            breaker.check()

    def test_success_resets(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.check()  # still closed

    def test_half_open_after_reset_window(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0.01)
        breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            breaker.check()
        time.sleep(0.02)
        breaker.check()  # probe allowed
