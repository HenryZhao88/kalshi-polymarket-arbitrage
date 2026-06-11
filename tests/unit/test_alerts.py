"""Alert payload rendering and sink transport tests (mocked HTTP)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from arb_scanner.app.alerts.base import AlertPayload
from arb_scanner.app.alerts.discord import DiscordAlertSink
from arb_scanner.app.alerts.telegram import TelegramAlertSink
from arb_scanner.app.fees.profit import FeeBreakdown
from arb_scanner.app.types import Money

D = Decimal

AiohttpClientFn = Callable[[web.Application], Awaitable[TestClient[Any, Any]]]


@pytest.fixture
async def aiohttp_client() -> AsyncIterator[AiohttpClientFn]:
    clients: list[TestClient[Any, Any]] = []

    async def factory(app: web.Application) -> TestClient[Any, Any]:
        client: TestClient[Any, Any] = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.close()


PAYLOAD = AlertPayload(
    kalshi_ticker="KXBTCD-26JUN30-T70000",
    poly_condition_id="0xabcdef1234567890",
    direction="kalshi_yes_poly_no",
    confidence=0.93,
    size=100,
    depth_summary="k_levels=1 p_levels=1",
    fees=FeeBreakdown(
        kalshi_fee=Money.from_dollars("0.63"),
        polymarket_fee=Money.from_dollars("0.1164"),
    ),
    net_edge=Money.from_dollars("6.2536"),
    simple_return=D("0.067"),
    annualized_return=D("0.81"),
    break_even_slippage_per_share=D("0.0625"),
    break_even_extra_fees=Money.from_dollars("6.2536"),
    snapshot_id=42,
)


class TestRenderText:
    def test_contains_every_required_field(self) -> None:
        text = PAYLOAD.render_text()
        assert "KXBTCD-26JUN30-T70000" in text
        assert "0.93" in text  # confidence
        assert "k_levels=1" in text  # depth summary
        assert "0.63" in text  # fee breakdown
        assert "6.2536" in text  # net edge
        assert "81" in text  # annualized %
        assert "break-even" in text
        assert "snapshot #42" in text


class TestDiscordSink:
    async def test_posts_to_webhook(self, aiohttp_client: AiohttpClientFn) -> None:
        received: list[dict[str, Any]] = []
        app = web.Application()

        async def hook(request: web.Request) -> web.Response:
            received.append(await request.json())
            return web.json_response({}, status=204)

        app.router.add_post("/webhook", hook)
        client = await aiohttp_client(app)
        assert client.session is not None
        sink = DiscordAlertSink(client.session, str(client.make_url("/webhook")))
        await sink.send(PAYLOAD)
        assert "KXBTCD" in received[0]["content"]


class TestTelegramSink:
    async def test_posts_to_bot_api(self, aiohttp_client: AiohttpClientFn) -> None:
        received: list[dict[str, Any]] = []
        app = web.Application()

        async def send_message(request: web.Request) -> web.Response:
            received.append(await request.json())
            return web.json_response({"ok": True})

        app.router.add_post("/bottoken/sendMessage", send_message)
        client = await aiohttp_client(app)
        assert client.session is not None
        sink = TelegramAlertSink(client.session, "token", "chat-1")
        sink._url = str(client.make_url("/bottoken/sendMessage"))  # noqa: SLF001
        await sink.send(PAYLOAD)
        assert received[0]["chat_id"] == "chat-1"
        assert "net" in received[0]["text"].lower()
