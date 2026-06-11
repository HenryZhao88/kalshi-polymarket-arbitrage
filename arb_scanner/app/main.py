"""CLI: scan / dry-run / replay / report."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import aiohttp

from arb_scanner.app.alerts.base import AlertSink
from arb_scanner.app.alerts.discord import DiscordAlertSink
from arb_scanner.app.alerts.telegram import TelegramAlertSink
from arb_scanner.app.clients.kalshi_rest import KalshiRestClient
from arb_scanner.app.clients.polymarket_clob import PolymarketClobClient
from arb_scanner.app.clients.polymarket_gamma import PolymarketGammaClient
from arb_scanner.app.config import Settings
from arb_scanner.app.scanner import scan_once


def _build_sinks(settings: Settings, session: aiohttp.ClientSession) -> list[AlertSink]:
    sinks: list[AlertSink] = []
    if settings.discord_webhook_url:
        sinks.append(DiscordAlertSink(session, settings.discord_webhook_url.get_secret_value()))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        sinks.append(
            TelegramAlertSink(
                session,
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_chat_id,
            )
        )
    return sinks


async def _run_pass(settings: Settings, *, verbose: bool) -> None:
    async with aiohttp.ClientSession() as session:
        report = await scan_once(
            kalshi=KalshiRestClient(session),
            gamma=PolymarketGammaClient(session),
            clob=PolymarketClobClient(session),
            sinks=_build_sinks(settings, session),
        )
        for line in report.render_lines():
            print(line)
        if verbose and not report.opportunities:
            print("no accepted pairs produced evaluable opportunities this pass")


async def _scan_loop(settings: Settings, interval_seconds: float) -> None:
    while True:
        try:
            await _run_pass(settings, verbose=False)
        except Exception:
            logging.getLogger("arb_scanner").exception("scan pass failed")
        await asyncio.sleep(interval_seconds)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-scanner")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="continuous discovery loop")
    scan.add_argument("--interval", type=float, default=60.0, help="seconds between passes")
    sub.add_parser("dry-run", help="one verbose pass, console output only")
    replay = sub.add_parser("replay", help="replay stored snapshots through the simulator")
    replay.add_argument("--database-url", default=None)
    report = sub.add_parser("report", help="render backtest metrics report")
    report.add_argument("--out", default="reports/report.html")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()

    if args.command == "dry-run":
        asyncio.run(_run_pass(settings, verbose=True))
        return 0
    if args.command == "scan":
        asyncio.run(_scan_loop(settings, args.interval))
        return 0
    if args.command in ("replay", "report"):
        from arb_scanner.app.backtest.replay import run_replay_cli

        return run_replay_cli(args, settings)
    return 1


if __name__ == "__main__":
    sys.exit(cli())
