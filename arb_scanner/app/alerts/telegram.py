"""Telegram bot alert sink."""

from __future__ import annotations

import aiohttp

from arb_scanner.app.alerts.base import AlertDeliveryError, AlertPayload


class TelegramAlertSink:
    def __init__(self, session: aiohttp.ClientSession, bot_token: str, chat_id: str) -> None:
        self._session = session
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, payload: AlertPayload) -> None:
        try:
            async with self._session.post(
                self._url,
                json={"chat_id": self._chat_id, "text": payload.render_text()},
            ) as resp:
                if resp.status >= 400:
                    raise AlertDeliveryError(f"Telegram alert failed with HTTP {resp.status}")
        except AlertDeliveryError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise AlertDeliveryError("Telegram alert transport failed") from None
