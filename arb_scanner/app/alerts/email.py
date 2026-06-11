"""SMTP email alert sink (sync smtplib pushed to a thread)."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from arb_scanner.app.alerts.base import AlertPayload


class EmailAlertSink:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        to_address: str,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = to_address

    def _send_sync(self, payload: AlertPayload) -> None:
        msg = EmailMessage()
        msg["Subject"] = (
            f"arb-scanner: net ${payload.net_edge.to_dollars()} {payload.kalshi_ticker}"
        )
        msg["From"] = self._user
        msg["To"] = self._to
        msg.set_content(payload.render_text())
        with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self._user, self._password)
            smtp.send_message(msg)

    async def send(self, payload: AlertPayload) -> None:
        await asyncio.to_thread(self._send_sync, payload)
