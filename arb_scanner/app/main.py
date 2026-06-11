"""CLI: scan / dry-run / replay / report."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp

from arb_scanner.app.alerts.base import AlertSink
from arb_scanner.app.alerts.discord import DiscordAlertSink
from arb_scanner.app.alerts.email import EmailAlertSink
from arb_scanner.app.alerts.telegram import TelegramAlertSink
from arb_scanner.app.clients.kalshi_rest import KalshiRestClient
from arb_scanner.app.clients.polymarket_clob import PolymarketClobClient
from arb_scanner.app.clients.polymarket_gamma import PolymarketGammaClient
from arb_scanner.app.config import Settings
from arb_scanner.app.economics import CostAssumptions
from arb_scanner.app.markets.discovery import ManualReviewSort
from arb_scanner.app.risk.controls import RiskLimits
from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.scanner import ScanReport, scan_once
from arb_scanner.app.storage.engine import init_models, make_engine, make_session_factory
from arb_scanner.app.storage.repo import SqlAlchemyScanStore
from arb_scanner.app.types import Money


def _build_sinks(
    settings: Settings, session: aiohttp.ClientSession, *, allow_external: bool
) -> list[AlertSink]:
    if not allow_external:
        return []
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
    if (
        settings.smtp_host
        and settings.smtp_user
        and settings.smtp_password
        and settings.alert_email_to
    ):
        sinks.append(
            EmailAlertSink(
                host=settings.smtp_host,
                port=settings.smtp_port,
                user=settings.smtp_user,
                password=settings.smtp_password.get_secret_value(),
                to_address=settings.alert_email_to,
            )
        )
    return sinks


def _money(value: Decimal | None) -> Money | None:
    return Money.from_dollars(value) if value is not None else None


def _cost_assumptions(settings: Settings) -> CostAssumptions:
    return CostAssumptions(
        bridge_cost=_money(settings.bridge_cost_dollars),
        withdrawal_cost=_money(settings.withdrawal_cost_dollars),
        gas_cost=_money(settings.gas_cost_dollars),
        processor_cost=_money(settings.processor_cost_dollars),
        conversion_cost=_money(settings.conversion_cost_dollars),
        unknown_cost_buffer=Money.from_dollars(settings.unknown_cost_buffer_dollars),
    )


def _risk_limits(settings: Settings) -> RiskLimits:
    return RiskLimits(
        allow_unknown_hold_time=settings.allow_unknown_hold_time,
        allow_unknown_quote_age=settings.allow_unknown_quote_age,
    )


def _build_kill_switch(settings: Settings) -> KillSwitch:
    return KillSwitch(flag_file=settings.kill_switch_file)


async def _scan_pass(
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    sinks: list[AlertSink],
    exposure: ExposureTracker,
    kill_switch: KillSwitch,
) -> ScanReport:
    kalshi = KalshiRestClient(session)
    gamma = PolymarketGammaClient(session)
    clob = PolymarketClobClient(session)

    async def run(store: SqlAlchemyScanStore | None = None) -> ScanReport:
        return await scan_once(
            kalshi=kalshi,
            gamma=gamma,
            clob=clob,
            sinks=sinks,
            limits=_risk_limits(settings),
            exposure=exposure,
            kill_switch=kill_switch,
            cost_assumptions=_cost_assumptions(settings),
            allow_unknown_fees=settings.allow_unknown_fees,
            allow_unknown_costs=settings.allow_unknown_costs,
            store=store,
            polymarket_page_size=settings.polymarket_page_size,
            polymarket_max_pages=settings.polymarket_max_pages,
            polymarket_max_markets=settings.polymarket_max_markets,
        )

    if not settings.persist_scans:
        return await run()

    engine = make_engine(settings.database_url)
    try:
        await init_models(engine)
        factory = make_session_factory(engine)
        async with factory() as db_session:
            store = SqlAlchemyScanStore(
                db_session,
                persist_raw_candidates=settings.persist_raw_candidates,
                max_candidates_per_scan=settings.storage_max_candidates_per_scan,
            )
            await store.apply_retention(
                datetime.now(UTC) - timedelta(days=settings.storage_retention_days)
            )
            report = await run(store)
            await db_session.commit()
            return report
    finally:
        await engine.dispose()


async def _run_pass(
    settings: Settings,
    *,
    verbose: bool,
    dry_run: bool,
    exposure: ExposureTracker,
    kill_switch: KillSwitch,
    show_manual_review: int = 0,
    manual_review_sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
) -> None:
    async with aiohttp.ClientSession() as session:
        allow_external = not dry_run or settings.dry_run_send_alerts
        report = await _scan_pass(
            settings,
            session,
            sinks=_build_sinks(settings, session, allow_external=allow_external),
            exposure=exposure,
            kill_switch=kill_switch,
        )
        for line in report.render_lines():
            print(line)
        for line in report.render_manual_review_lines(show_manual_review, sort=manual_review_sort):
            print(line)
        if verbose and not report.opportunities:
            print("no accepted pairs produced evaluable opportunities this pass")


async def _scan_loop(settings: Settings, interval_seconds: float) -> None:
    exposure = ExposureTracker()
    kill_switch = _build_kill_switch(settings)
    while True:
        try:
            await _run_pass(
                settings,
                verbose=False,
                dry_run=False,
                exposure=exposure,
                kill_switch=kill_switch,
                show_manual_review=0,
                manual_review_sort=ManualReviewSort.SIMILARITY,
            )
        except Exception:
            logging.getLogger("arb_scanner").exception("scan pass failed")
        await asyncio.sleep(interval_seconds)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-scanner")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="continuous discovery loop")
    scan.add_argument("--interval", type=float, default=60.0, help="seconds between passes")
    dry_run = sub.add_parser("dry-run", help="one console-only pass unless explicitly configured")
    dry_run.add_argument(
        "--show-manual-review",
        type=int,
        default=0,
        metavar="N",
        help="print the top N unsafe manual-review candidates",
    )
    dry_run.add_argument(
        "--manual-review-sort",
        choices=[mode.value for mode in ManualReviewSort],
        default=ManualReviewSort.SIMILARITY.value,
        help="ranking for unsafe manual-review output",
    )
    replay = sub.add_parser("replay", help="re-evaluate stored paired opportunity snapshots")
    replay.add_argument("--database-url", default=None)
    report = sub.add_parser("report", help="render paired-snapshot replay metrics")
    report.add_argument("--database-url", default=None)
    report.add_argument("--out", default="reports/report.html")
    report_mode = report.add_mutually_exclusive_group()
    report_mode.add_argument("--latest", action="store_true")
    report_mode.add_argument("--manual-review", action="store_true")
    report_mode.add_argument("--rejections", action="store_true")
    report.add_argument("--limit", type=int, default=20)
    report.add_argument(
        "--sort",
        choices=[mode.value for mode in ManualReviewSort],
        default=ManualReviewSort.SIMILARITY.value,
        help="ranking for manual-review reports",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()

    if args.command == "dry-run":
        asyncio.run(
            _run_pass(
                settings,
                verbose=True,
                dry_run=True,
                exposure=ExposureTracker(),
                kill_switch=_build_kill_switch(settings),
                show_manual_review=args.show_manual_review,
                manual_review_sort=ManualReviewSort(args.manual_review_sort),
            )
        )
        return 0
    if args.command == "scan":
        asyncio.run(_scan_loop(settings, args.interval))
        return 0
    if args.command == "report" and (args.latest or args.manual_review or args.rejections):
        from arb_scanner.app.storage.reporting import run_diagnostic_report

        mode = "latest" if args.latest else "manual_review" if args.manual_review else "rejected"
        return run_diagnostic_report(
            args.database_url or settings.database_url,
            mode=mode,
            limit=args.limit,
            sort=ManualReviewSort(args.sort),
        )
    if args.command in ("replay", "report"):
        from arb_scanner.app.backtest.replay import run_replay_cli

        return run_replay_cli(args, settings)
    return 1


if __name__ == "__main__":
    sys.exit(cli())
