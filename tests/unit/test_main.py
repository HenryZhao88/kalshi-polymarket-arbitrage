"""CLI wiring tests for safe dry-run defaults and the file kill switch."""

from pathlib import Path
from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

import arb_scanner.app.main as main_module
import arb_scanner.app.storage.reporting as reporting_module
from arb_scanner.app.alerts.base import AlertSink
from arb_scanner.app.config import Settings
from arb_scanner.app.main import _build_kill_switch, _build_sinks, _run_pass
from arb_scanner.app.markets.discovery import ManualReviewSort
from arb_scanner.app.risk.exposure import ExposureTracker
from arb_scanner.app.risk.kill_switch import KillSwitch
from arb_scanner.app.scanner import ScanReport


async def test_dry_run_builds_no_external_sinks_by_default() -> None:
    settings = Settings(
        _env_file=None,
        discord_webhook_url=SecretStr("https://discord.invalid/private-token"),
        telegram_bot_token=SecretStr("private-token"),
        telegram_chat_id="chat",
    )
    async with aiohttp.ClientSession() as session:
        assert _build_sinks(settings, session, allow_external=False) == []


def test_cli_kill_switch_uses_configured_file(tmp_path: Path) -> None:
    flag = tmp_path / "scanner.stop"
    settings = Settings(_env_file=None, kill_switch_file=flag)
    switch = _build_kill_switch(settings)
    assert not switch.engaged
    flag.touch()
    assert switch.engaged


async def test_dry_run_path_forwards_file_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = tmp_path / "scanner.stop"
    flag.touch()
    settings = Settings(_env_file=None, kill_switch_file=flag, persist_scans=False)
    observed = {"engaged": False}

    async def fake_scan_pass(
        settings: Settings,
        session: aiohttp.ClientSession,
        *,
        sinks: list[AlertSink],
        exposure: ExposureTracker,
        kill_switch: KillSwitch,
    ) -> ScanReport:
        observed["engaged"] = kill_switch.engaged
        return ScanReport()

    monkeypatch.setattr(main_module, "_scan_pass", fake_scan_pass)
    await _run_pass(
        settings,
        verbose=False,
        dry_run=True,
        exposure=ExposureTracker(),
        kill_switch=_build_kill_switch(settings),
    )
    assert observed["engaged"]


def test_cli_forwards_manual_review_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    async def fake_run_pass(
        settings: Settings,
        *,
        verbose: bool,
        dry_run: bool,
        exposure: ExposureTracker,
        kill_switch: KillSwitch,
        show_manual_review: int = 0,
        manual_review_sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
    ) -> None:
        observed.update(
            verbose=verbose,
            dry_run=dry_run,
            show_manual_review=show_manual_review,
            manual_review_sort=manual_review_sort,
        )

    monkeypatch.setattr(main_module, "_run_pass", fake_run_pass)

    assert main_module.cli(["dry-run", "--show-manual-review", "7"]) == 0
    assert observed == {
        "verbose": True,
        "dry_run": True,
        "show_manual_review": 7,
        "manual_review_sort": ManualReviewSort.SIMILARITY,
    }


def test_cli_forwards_manual_review_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    async def fake_run_pass(
        settings: Settings,
        *,
        verbose: bool,
        dry_run: bool,
        exposure: ExposureTracker,
        kill_switch: KillSwitch,
        show_manual_review: int = 0,
        manual_review_sort: ManualReviewSort = ManualReviewSort.SIMILARITY,
    ) -> None:
        observed["sort"] = manual_review_sort

    monkeypatch.setattr(main_module, "_run_pass", fake_run_pass)
    assert (
        main_module.cli(
            [
                "dry-run",
                "--show-manual-review",
                "5",
                "--manual-review-sort",
                "missing_fields",
            ]
        )
        == 0
    )
    assert observed["sort"] is ManualReviewSort.MISSING_FIELDS


@pytest.mark.parametrize(
    ("flag", "expected_mode"),
    [
        ("--latest", "latest"),
        ("--manual-review", "manual_review"),
        ("--rejections", "rejected"),
    ],
)
def test_cli_routes_diagnostic_reports(
    flag: str,
    expected_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_report(
        database_url: str,
        *,
        mode: str,
        limit: int,
        sort: ManualReviewSort,
        fmt: str = "text",
        output: str | None = None,
        verification_packet: bool = False,
    ) -> int:
        observed.update(
            database_url=database_url,
            mode=mode,
            limit=limit,
            sort=sort,
            fmt=fmt,
            output=output,
            verification_packet=verification_packet,
        )
        return 0

    monkeypatch.setattr(reporting_module, "run_diagnostic_report", fake_report)

    assert (
        main_module.cli(["report", flag, "--limit", "12", "--database-url", "sqlite+aiosqlite://"])
        == 0
    )
    assert observed == {
        "database_url": "sqlite+aiosqlite://",
        "mode": expected_mode,
        "limit": 12,
        "sort": ManualReviewSort.SIMILARITY,
        "fmt": "text",
        "output": None,
        "verification_packet": False,
    }


def _capture_report(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    def fake_report(database_url: str, **kwargs: Any) -> int:
        observed.update(database_url=database_url, **kwargs)
        return 0

    monkeypatch.setattr(reporting_module, "run_diagnostic_report", fake_report)
    return observed


def test_cli_forwards_report_format_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = _capture_report(monkeypatch)
    assert (
        main_module.cli(
            [
                "report",
                "--manual-review",
                "--limit",
                "50",
                "--sort",
                "missing_fields",
                "--format",
                "csv",
                "--output",
                "manual_review.csv",
                "--database-url",
                "sqlite+aiosqlite://",
            ]
        )
        == 0
    )
    assert observed["fmt"] == "csv"
    assert observed["output"] == "manual_review.csv"
    assert observed["sort"] is ManualReviewSort.MISSING_FIELDS
    assert observed["verification_packet"] is False


def test_cli_report_format_defaults_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = _capture_report(monkeypatch)
    assert (
        main_module.cli(
            ["report", "--manual-review", "--database-url", "sqlite+aiosqlite://"]
        )
        == 0
    )
    assert observed["fmt"] == "text"
    assert observed["output"] is None


def test_cli_verification_packet_implies_manual_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _capture_report(monkeypatch)
    assert (
        main_module.cli(
            [
                "report",
                "--verification-packet",
                "--limit",
                "10",
                "--database-url",
                "sqlite+aiosqlite://",
            ]
        )
        == 0
    )
    assert observed["mode"] == "manual_review"
    assert observed["verification_packet"] is True


@pytest.mark.parametrize(
    "sort",
    ["confidence", "market_type", "fee_confidence", "hypothetical_edge", "event_date"],
)
def test_cli_report_accepts_all_sort_modes(sort: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = _capture_report(monkeypatch)
    assert (
        main_module.cli(
            [
                "report",
                "--manual-review",
                "--sort",
                sort,
                "--database-url",
                "sqlite+aiosqlite://",
            ]
        )
        == 0
    )
    assert observed["sort"] is ManualReviewSort(sort)


def test_cli_routes_cleanup_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    def fake_cleanup(database_url: str, *, retention_days: int) -> int:
        observed.update(database_url=database_url, retention_days=retention_days)
        return 0

    monkeypatch.setattr(reporting_module, "run_retention_cleanup", fake_cleanup)
    assert (
        main_module.cli(
            ["report", "--cleanup-retention", "--database-url", "sqlite+aiosqlite://"]
        )
        == 0
    )
    assert observed["database_url"] == "sqlite+aiosqlite://"
    assert observed["retention_days"] == Settings(_env_file=None).storage_retention_days


def test_cli_has_no_execution_command() -> None:
    """The CLI must never grow a trade/execute/order entry point."""
    for forbidden in ("trade", "execute", "order", "buy", "sell"):
        with pytest.raises(SystemExit):
            main_module.cli([forbidden])
