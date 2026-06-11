"""Discord webhook alert sink."""

from __future__ import annotations

import aiohttp

from arb_scanner.app.alerts.base import AlertDeliveryError, AlertPayload


class DiscordAlertSink:
    def __init__(self, session: aiohttp.ClientSession, webhook_url: str) -> None:
        self._session = session
        self._webhook_url = webhook_url

    async def send(self, payload: AlertPayload) -> None:
        try:
            async with self._session.post(
                self._webhook_url,
                json={"content": f"```\n{payload.render_text()}\n```"},
            ) as resp:
                if resp.status >= 400:
                    raise AlertDeliveryError(f"Discord alert failed with HTTP {resp.status}")
        except AlertDeliveryError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise AlertDeliveryError("Discord alert transport failed") from None
