"""Discord webhook alert sink."""

from __future__ import annotations

import aiohttp

from arb_scanner.app.alerts.base import AlertPayload


class DiscordAlertSink:
    def __init__(self, session: aiohttp.ClientSession, webhook_url: str) -> None:
        self._session = session
        self._webhook_url = webhook_url

    async def send(self, payload: AlertPayload) -> None:
        async with self._session.post(
            self._webhook_url,
            json={"content": f"```\n{payload.render_text()}\n```"},
        ) as resp:
            resp.raise_for_status()
