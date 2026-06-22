"""REST client integration tests against a local aiohttp server serving the
committed live fixtures (captured 2026-06-11)."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arb_scanner.app.clients.base import AuthError, NotFoundError, RateLimitedError, VenueError
from arb_scanner.app.clients.geoblock import (
    ExecutionDisabledError,
    GeoblockClient,
    ensure_execution_allowed,
)
from arb_scanner.app.clients.kalshi_rest import KalshiRestClient, KalshiSigner
from arb_scanner.app.clients.polymarket_clob import PolymarketClobClient
from arb_scanner.app.clients.polymarket_gamma import PolymarketGammaClient
from arb_scanner.app.config import Mode, Settings

FIXTURES = Path("tests/fixtures/live_2026-06-11")

AiohttpClientFn = Callable[[web.Application], Awaitable[TestClient[Any, Any]]]


@pytest.fixture
async def aiohttp_client() -> Any:
    clients: list[TestClient[Any, Any]] = []

    async def factory(app: web.Application) -> TestClient[Any, Any]:
        client: TestClient[Any, Any] = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.close()


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


async def make_kalshi(aiohttp_client: AiohttpClientFn) -> KalshiRestClient:
    app = web.Application()

    async def markets(request: web.Request) -> web.Response:
        return web.json_response(fixture_json("kalshi_markets.json"))

    async def orderbook(request: web.Request) -> web.Response:
        if request.match_info["ticker"] == "MISSING":
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(fixture_json("kalshi_orderbook_nonempty.json"))

    async def fee_changes(request: web.Request) -> web.Response:
        return web.json_response(fixture_json("kalshi_fee_changes.json"))

    app.router.add_get("/trade-api/v2/markets", markets)
    app.router.add_get("/trade-api/v2/markets/{ticker}/orderbook", orderbook)
    app.router.add_get("/trade-api/v2/series/fee_changes", fee_changes)
    client = await aiohttp_client(app)
    assert client.session is not None
    return KalshiRestClient(client.session, base_url=str(client.make_url("")))


class TestKalshiRest:
    async def test_get_markets(self, aiohttp_client: AiohttpClientFn) -> None:
        kalshi = await make_kalshi(aiohttp_client)
        result = await kalshi.get_markets(limit=3)
        assert "markets" in result and len(result["markets"]) > 0

    async def test_get_all_markets_paginates_and_excludes_mve(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        requests: list[dict[str, str]] = []

        async def markets(request: web.Request) -> web.Response:
            requests.append(dict(request.query))
            assert request.query["mve_filter"] == "exclude"
            if request.query.get("cursor") is None:
                return web.json_response({"markets": [{"ticker": "FIRST"}], "cursor": "page-2"})
            assert request.query["cursor"] == "page-2"
            return web.json_response({"markets": [{"ticker": "SECOND"}], "cursor": ""})

        app = web.Application()
        app.router.add_get("/trade-api/v2/markets", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))

        result = await kalshi.get_all_markets(limit=100, mve_filter="exclude")

        assert [market["ticker"] for market in result] == ["FIRST", "SECOND"]
        assert len(requests) == 2
        assert requests[1]["cursor"] == "page-2"

    async def test_get_all_markets_stops_at_max_pages_without_raising(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        # An inexhaustible feed (always a fresh cursor): hitting the page cap is
        # a graceful stop that returns what was collected, not a scan-killing
        # error. The cap is a safety guardrail for full-venue coverage.
        async def markets(request: web.Request) -> web.Response:
            cursor = request.query.get("cursor") or "0"
            return web.json_response(
                {"markets": [{"ticker": f"M{cursor}"}], "cursor": str(int(cursor) + 1)}
            )

        app = web.Application()
        app.router.add_get("/trade-api/v2/markets", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))

        result = await kalshi.get_all_markets(limit=100, max_pages=3)
        assert [market["ticker"] for market in result] == ["M0", "M1", "M2"]

    async def test_get_all_markets_rejects_repeated_cursor(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        async def markets(request: web.Request) -> web.Response:
            return web.json_response({"markets": [], "cursor": "same"})

        app = web.Application()
        app.router.add_get("/trade-api/v2/markets", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))

        with pytest.raises(VenueError, match="repeated cursor"):
            await kalshi.get_all_markets()

    async def test_get_orderbook_fixture_roundtrip(self, aiohttp_client: AiohttpClientFn) -> None:
        kalshi = await make_kalshi(aiohttp_client)
        payload = await kalshi.get_orderbook("KXNASDAQ100Y-26DEC31H1600-T33000")
        assert payload["orderbook_fp"]["yes_dollars"]

    async def test_404_maps_to_not_found(self, aiohttp_client: AiohttpClientFn) -> None:
        kalshi = await make_kalshi(aiohttp_client)
        with pytest.raises(NotFoundError):
            await kalshi.get_orderbook("MISSING")

    async def test_get_series_fee_changes(self, aiohttp_client: AiohttpClientFn) -> None:
        kalshi = await make_kalshi(aiohttp_client)
        result = await kalshi.get_series_fee_changes()
        assert "series_fee_change_arr" in result


def _signer() -> KalshiSigner:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return KalshiSigner("test-key-id", pem)


class TestKalshiOrders:
    async def test_private_endpoint_without_signer_raises_auth_error(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        # No network should be touched: the missing-signer guard fails closed.
        app = web.Application()
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))
        with pytest.raises(AuthError):
            await kalshi.get_balance()

    async def test_create_order_signs_and_sends_body(self, aiohttp_client: AiohttpClientFn) -> None:
        received: dict[str, Any] = {}

        async def create(request: web.Request) -> web.Response:
            received["headers"] = dict(request.headers)
            received["body"] = await request.json()
            return web.json_response(
                {"order": {"order_id": "ord-1", "status": "resting"}}, status=201
            )

        app = web.Application()
        app.router.add_post("/trade-api/v2/portfolio/orders", create)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(
            client.session, base_url=str(client.make_url("")), signer=_signer()
        )
        result = await kalshi.create_order(
            ticker="KXBTC-26JUN30-T70000",
            side="yes",
            action="buy",
            count=10,
            yes_price_cents=42,
            client_order_id="abc123",
        )
        assert result["order"]["order_id"] == "ord-1"
        # The request was signed and carried the structured order body.
        assert received["headers"]["KALSHI-ACCESS-KEY"] == "test-key-id"
        assert "KALSHI-ACCESS-SIGNATURE" in received["headers"]
        assert received["body"] == {
            "ticker": "KXBTC-26JUN30-T70000",
            "side": "yes",
            "action": "buy",
            "count": 10,
            "type": "limit",
            "yes_price": 42,
            "client_order_id": "abc123",
        }

    async def test_get_balance_returns_cents(self, aiohttp_client: AiohttpClientFn) -> None:
        async def balance(request: web.Request) -> web.Response:
            assert "KALSHI-ACCESS-SIGNATURE" in request.headers
            return web.json_response({"balance": 123456})

        app = web.Application()
        app.router.add_get("/trade-api/v2/portfolio/balance", balance)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(
            client.session, base_url=str(client.make_url("")), signer=_signer()
        )
        result = await kalshi.get_balance()
        assert result["balance"] == 123456

    async def test_cancel_order_uses_delete(self, aiohttp_client: AiohttpClientFn) -> None:
        seen: dict[str, str] = {}

        async def cancel(request: web.Request) -> web.Response:
            seen["method"] = request.method
            seen["order_id"] = request.match_info["order_id"]
            return web.json_response({"order": {"order_id": "ord-1", "status": "canceled"}})

        app = web.Application()
        app.router.add_delete("/trade-api/v2/portfolio/orders/{order_id}", cancel)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(
            client.session, base_url=str(client.make_url("")), signer=_signer()
        )
        result = await kalshi.cancel_order("ord-1")
        assert seen == {"method": "DELETE", "order_id": "ord-1"}
        assert result["order"]["status"] == "canceled"


class TestRetries:
    async def test_retries_on_429_then_succeeds(self, aiohttp_client: AiohttpClientFn) -> None:
        calls = {"n": 0}

        async def flaky(request: web.Request) -> web.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return web.json_response({"error": "too many requests"}, status=429)
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/trade-api/v2/exchange/status", flaky)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))
        result = await kalshi.get_exchange_status()
        assert result == {"ok": True}
        assert calls["n"] == 3

    async def test_gives_up_after_max_attempts(self, aiohttp_client: AiohttpClientFn) -> None:
        async def always_429(request: web.Request) -> web.Response:
            return web.json_response({"error": "too many requests"}, status=429)

        app = web.Application()
        app.router.add_get("/trade-api/v2/exchange/status", always_429)
        client = await aiohttp_client(app)
        assert client.session is not None
        kalshi = KalshiRestClient(client.session, base_url=str(client.make_url("")))
        with pytest.raises(RateLimitedError):
            await kalshi.get_exchange_status()


class TestPolymarketClob:
    async def test_get_book_fixture_roundtrip(self, aiohttp_client: AiohttpClientFn) -> None:
        app = web.Application()

        async def book(request: web.Request) -> web.Response:
            assert request.query["token_id"]
            return web.json_response(fixture_json("poly_book.json"))

        app.router.add_get("/book", book)
        client = await aiohttp_client(app)
        assert client.session is not None
        clob = PolymarketClobClient(client.session, base_url=str(client.make_url("")))
        payload = await clob.get_book("78433024518676680431174478322854148606578065650008")
        assert payload["bids"]

    async def test_get_market_info_uses_clob_market_route(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        app = web.Application()

        async def market_info(request: web.Request) -> web.Response:
            assert request.match_info["condition_id"] == "0xabc"
            return web.json_response({"fd": {"r": "0.05", "e": "1", "to": True}})

        app.router.add_get("/clob-markets/{condition_id}", market_info)
        client = await aiohttp_client(app)
        assert client.session is not None
        clob = PolymarketClobClient(client.session, base_url=str(client.make_url("")))
        payload = await clob.get_market_info("0xabc")
        assert payload["fd"]["r"] == "0.05"


class TestPolymarketGamma:
    async def test_keyset_pagination_fetches_multiple_pages(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        cursors: list[str | None] = []

        async def markets(request: web.Request) -> web.Response:
            cursor = request.query.get("after_cursor")
            cursors.append(cursor)
            if cursor is None:
                return web.json_response(
                    {
                        "markets": [{"conditionId": "first"}],
                        "next_cursor": "page-2",
                    }
                )
            return web.json_response({"markets": [{"conditionId": "second"}], "next_cursor": ""})

        app = web.Application()
        app.router.add_get("/markets/keyset", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        gamma = PolymarketGammaClient(client.session, base_url=str(client.make_url("")))

        result = await gamma.get_all_markets(page_size=100, max_pages=5, max_markets=500)

        assert [market["conditionId"] for market in result.markets] == ["first", "second"]
        assert result.pages_fetched == 2
        assert result.total_fetched == 2
        assert cursors == [None, "page-2"]

    async def test_keyset_pagination_rejects_repeated_cursor(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        calls = 0

        async def markets(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.json_response(
                {
                    "markets": [{"conditionId": f"market-{calls}"}],
                    "next_cursor": "same",
                }
            )

        app = web.Application()
        app.router.add_get("/markets/keyset", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        gamma = PolymarketGammaClient(client.session, base_url=str(client.make_url("")))

        with pytest.raises(VenueError, match="repeated cursor"):
            await gamma.get_all_markets()

    async def test_keyset_pagination_rejects_repeated_page(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        calls = 0

        async def markets(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.json_response(
                {
                    "markets": [{"conditionId": "duplicate"}],
                    "next_cursor": f"cursor-{calls}",
                }
            )

        app = web.Application()
        app.router.add_get("/markets/keyset", markets)
        client = await aiohttp_client(app)
        assert client.session is not None
        gamma = PolymarketGammaClient(client.session, base_url=str(client.make_url("")))

        with pytest.raises(VenueError, match="repeated a market page"):
            await gamma.get_all_markets()


class TestGeoblockGate:
    @staticmethod
    def app_with_geoblock(blocked: bool) -> web.Application:
        app = web.Application()

        async def geoblock(request: web.Request) -> web.Response:
            return web.json_response(
                {"blocked": blocked, "ip": "203.0.113.42", "country": "US", "region": "NY"}
            )

        app.router.add_get("/api/geoblock", geoblock)
        return app

    async def test_discovery_mode_always_disabled(self, aiohttp_client: AiohttpClientFn) -> None:
        client = await aiohttp_client(self.app_with_geoblock(blocked=False))
        assert client.session is not None
        geo = GeoblockClient(client.session, base_url=str(client.make_url("")))
        settings = Settings(_env_file=None)
        with pytest.raises(ExecutionDisabledError, match="discovery-only"):
            await ensure_execution_allowed(settings, geo)

    async def test_blocked_region_disables_even_in_execution_mode(
        self, aiohttp_client: AiohttpClientFn
    ) -> None:
        client = await aiohttp_client(self.app_with_geoblock(blocked=True))
        assert client.session is not None
        geo = GeoblockClient(client.session, base_url=str(client.make_url("")))
        settings = Settings(_env_file=None, mode=Mode.EXECUTION_ENABLED)
        with pytest.raises(ExecutionDisabledError, match="hard-disabled"):
            await ensure_execution_allowed(settings, geo)

    async def test_geoblock_failure_fails_closed(self, aiohttp_client: AiohttpClientFn) -> None:
        app = web.Application()  # no geoblock route → 404
        client = await aiohttp_client(app)
        assert client.session is not None
        geo = GeoblockClient(client.session, base_url=str(client.make_url("")))
        settings = Settings(_env_file=None, mode=Mode.EXECUTION_ENABLED)
        with pytest.raises(ExecutionDisabledError, match="failing closed"):
            await ensure_execution_allowed(settings, geo)

    async def test_eligible_passes(self, aiohttp_client: AiohttpClientFn) -> None:
        client = await aiohttp_client(self.app_with_geoblock(blocked=False))
        assert client.session is not None
        geo = GeoblockClient(client.session, base_url=str(client.make_url("")))
        settings = Settings(_env_file=None, mode=Mode.EXECUTION_ENABLED)
        await ensure_execution_allowed(settings, geo)  # no raise


class TestLiveSmoke:
    """Live read-only smoke test; requires unrestricted network (VPN/VPS).
    Run with: uv run pytest -m live"""

    @pytest.mark.live
    async def test_public_reads(self) -> None:
        async with aiohttp.ClientSession() as session:
            kalshi = KalshiRestClient(session)
            status = await kalshi.get_exchange_status()
            assert "exchange_active" in status
            clob = PolymarketClobClient(session)
            markets = await clob.get_sampling_markets()
            assert markets["data"]
