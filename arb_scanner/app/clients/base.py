"""Shared client infrastructure: error taxonomy, rate limiters, circuit breaker,
and a retrying aiohttp REST base with correlation-ID structured logging."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


class VenueError(Exception):
    """Base for venue API failures."""


class BadRequestError(VenueError):  # 400
    pass


class AuthError(VenueError):  # 401/403
    pass


class NotFoundError(VenueError):  # 404
    pass


class TooEarlyError(VenueError):  # 425
    pass


class RateLimitedError(VenueError):  # 429 — retryable
    pass


class ServerError(VenueError):  # 5xx — retryable
    pass


class CircuitOpenError(VenueError):
    """Circuit breaker is open; venue is considered unavailable."""


_STATUS_ERRORS: dict[int, type[VenueError]] = {
    400: BadRequestError,
    401: AuthError,
    403: AuthError,
    404: NotFoundError,
    425: TooEarlyError,
    429: RateLimitedError,
}


def error_for_status(status: int, detail: str) -> VenueError:
    if status >= 500:
        return ServerError(detail)
    return _STATUS_ERRORS.get(status, VenueError)(detail)


@dataclass
class TokenBucket:
    """Kalshi-style token bucket: requests cost tokens; bucket refills at
    `refill_rate`/s up to `capacity`. No Retry-After on 429, so callers must also
    back off (docs.kalshi.com/getting_started/rate_limits, 2026-06-11)."""

    refill_rate: float
    capacity: float
    _tokens: float = field(init=False)
    _updated: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.refill_rate)
        self._updated = now

    async def acquire(self, cost: float = 10.0) -> None:
        """Default cost 10: most Kalshi endpoints cost 10 tokens."""
        while True:
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return
            deficit = cost - self._tokens
            await asyncio.sleep(deficit / self.refill_rate)


@dataclass
class SlidingWindowLimiter:
    """Polymarket-style sliding window (N requests / window seconds)."""

    max_requests: int
    window_seconds: float
    _stamps: deque[float] = field(default_factory=deque)

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._stamps and self._stamps[0] <= now - self.window_seconds:
                self._stamps.popleft()
            if len(self._stamps) < self.max_requests:
                self._stamps.append(now)
                return
            await asyncio.sleep(self._stamps[0] + self.window_seconds - now)


@dataclass
class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; half-opens after
    `reset_seconds` to allow one probe."""

    failure_threshold: int = 5
    reset_seconds: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None

    def check(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            return  # half-open: allow a probe
        raise CircuitOpenError(f"circuit open after {self._failures} consecutive failures")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


class RestClient:
    """Thin aiohttp wrapper: rate-limit → circuit-check → request → status mapping,
    with tenacity exponential backoff + jitter on retryable failures."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        *,
        name: str,
        bucket: TokenBucket | None = None,
        window: SlidingWindowLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._bucket = bucket
        self._window = window
        self._breaker = breaker or CircuitBreaker()
        self._max_attempts = max_attempts
        self._log = logging.getLogger(f"arb_scanner.clients.{name}")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cost: float = 10.0,
    ) -> Any:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(
                (RateLimitedError, ServerError, aiohttp.ClientError, asyncio.TimeoutError)
            ),
            wait=wait_random_exponential(multiplier=0.5, max=15),
            stop=stop_after_attempt(self._max_attempts),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._request_once(
                    method, path, params=params, json_body=json_body, headers=headers, cost=cost
                )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str] | None,
        cost: float,
    ) -> Any:
        self._breaker.check()
        if self._bucket is not None:
            await self._bucket.acquire(cost)
        if self._window is not None:
            await self._window.acquire()
        url = f"{self._base_url}{path}"
        cid = correlation_id.get()
        try:
            async with self._session.request(
                method, url, params=params, json=json_body, headers=headers
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    self._log.warning(
                        "request failed", extra={"cid": cid, "url": url, "status": resp.status}
                    )
                    err = error_for_status(resp.status, f"{resp.status} {url}: {detail}")
                    if isinstance(err, ServerError | RateLimitedError):
                        self._breaker.record_failure()
                    raise err
                self._breaker.record_success()
                return await resp.json()
        except aiohttp.ClientError:
            self._breaker.record_failure()
            raise
