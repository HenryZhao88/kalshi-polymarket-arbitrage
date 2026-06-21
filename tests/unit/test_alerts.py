"""Alert payload rendering and sink transport tests (mocked HTTP)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from arb_scanner.app.alerts.base import AlertDeliveryError, AlertPayload
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

    def test_persisted_payload_contains_cost_components(self) -> None:
        payload = PAYLOAD.to_dict()
        assert payload["kalshi_fee_dollars"] == "0.63"
        assert payload["polymarket_fee_dollars"] == "0.1164"
        assert payload["unknown_cost_buffer_dollars"] == "0"

    def test_risk_flags_are_rendered_and_persisted(self) -> None:
        flagged = AlertPayload(
            kalshi_ticker="KXBTCD-26JUN30-T70000",
            poly_condition_id="0xabcdef1234567890",
            direction="kalshi_yes_poly_no",
            confidence=0.93,
            size=100,
            depth_summary="k_levels=1 p_levels=1",
            fees=FeeBreakdown(),
            net_edge=Money.from_dollars("6.25"),
            simple_return=D("0.067"),
            annualized_return=D("0.81"),
            break_even_slippage_per_share=D("0.0625"),
            break_even_extra_fees=Money.from_dollars("6.25"),
            snapshot_id=42,
            risk_flags=(
                "resolution source unverified on at least one venue",
                "UMA challenge window: Polymarket outcome can be disputed post-resolution",
            ),
        )
        text = flagged.render_text()
        assert "VERIFY" in text.upper()
        assert "resolution source unverified" in text
        assert "UMA challenge window" in text
        persisted = flagged.to_dict()
        assert persisted["risk_flags"] == [
            "resolution source unverified on at least one venue",
            "UMA challenge window: Polymarket outcome can be disputed post-resolution",
        ]

    def test_no_risk_flags_renders_cleanly(self) -> None:
        assert PAYLOAD.to_dict()["risk_flags"] == []


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

    async def test_error_does_not_expose_webhook_url(self, aiohttp_client: AiohttpClientFn) -> None:
        app = web.Application()

        async def hook(request: web.Request) -> web.Response:
            return web.Response(status=500, text="private response body")

        app.router.add_post("/private-webhook-token", hook)
        client = await aiohttp_client(app)
        assert client.session is not None
        sink = DiscordAlertSink(client.session, str(client.make_url("/private-webhook-token")))
        with pytest.raises(AlertDeliveryError) as error:
            await sink.send(PAYLOAD)
        assert "private-webhook-token" not in str(error.value)
        assert "private response body" not in str(error.value)

    async def test_transport_error_is_sanitized(self) -> None:
        async with aiohttp.ClientSession() as session:
            sink = DiscordAlertSink(session, "private-token-not-a-url")
            with pytest.raises(AlertDeliveryError) as error:
                await sink.send(PAYLOAD)
        assert "private-token" not in str(error.value)


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
        sink._url = str(client.make_url("/bottoken/sendMessage"))
        await sink.send(PAYLOAD)
        assert received[0]["chat_id"] == "chat-1"
        assert "net" in received[0]["text"].lower()

    async def test_error_does_not_expose_bot_token(self, aiohttp_client: AiohttpClientFn) -> None:
        app = web.Application()

        async def fail(request: web.Request) -> web.Response:
            return web.Response(status=401, text="token rejected")

        app.router.add_post("/botprivate-token/sendMessage", fail)
        client = await aiohttp_client(app)
        assert client.session is not None
        sink = TelegramAlertSink(client.session, "private-token", "chat-1")
        sink._url = str(client.make_url("/botprivate-token/sendMessage"))
        with pytest.raises(AlertDeliveryError) as error:
            await sink.send(PAYLOAD)
        assert "private-token" not in str(error.value)
        assert "token rejected" not in str(error.value)
