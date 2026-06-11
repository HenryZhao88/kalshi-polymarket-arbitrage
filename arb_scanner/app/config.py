"""Application settings via pydantic-settings.

All values come from environment variables prefixed `ARB_` (or a local `.env`).
Secrets are `SecretStr` so they never appear in logs or reprs. Thresholds and
risk limits are added here, never hardcoded at call sites.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(StrEnum):
    DISCOVERY_ONLY = "discovery-only"
    EXECUTION_ENABLED = "execution-enabled"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Mode: execution additionally requires a passing runtime geoblock check
    # (clients/geoblock.py) before any Polymarket order path is enabled.
    mode: Mode = Mode.DISCOVERY_ONLY

    database_url: str = "sqlite+aiosqlite:///arb_scanner.db"
    persist_scans: bool = True
    persist_raw_candidates: bool = False
    storage_retention_days: int = Field(default=30, ge=1, le=3650)
    storage_max_candidates_per_scan: int = Field(default=5000, ge=1, le=1_000_000)
    kill_switch_file: Path | None = Path(".arb-scanner.kill")
    polymarket_max_markets: int = Field(default=500, ge=101, le=50_000)
    polymarket_page_size: int = Field(default=100, ge=1, le=100)
    polymarket_max_pages: int = Field(default=5, ge=1, le=500)

    # Unknown inputs fail closed unless explicitly allowed. Dollar-valued cost
    # settings are per evaluated two-leg opportunity; leaving one unset marks it
    # unknown rather than silently assuming zero.
    allow_unknown_fees: bool = False
    allow_unknown_costs: bool = False
    allow_unknown_hold_time: bool = False
    allow_unknown_quote_age: bool = False
    bridge_cost_dollars: Decimal | None = None
    withdrawal_cost_dollars: Decimal | None = None
    gas_cost_dollars: Decimal | None = None
    processor_cost_dollars: Decimal | None = None
    conversion_cost_dollars: Decimal | None = None
    unknown_cost_buffer_dollars: Decimal = Decimal("0")

    # Kalshi auth (RSA-PSS API key)
    kalshi_api_key_id: SecretStr | None = None
    kalshi_private_key_path: str | None = None

    # Polymarket auth (execution path only)
    polymarket_private_key: SecretStr | None = None
    polymarket_api_key: SecretStr | None = None
    polymarket_api_secret: SecretStr | None = None
    polymarket_api_passphrase: SecretStr | None = None

    # Alerts
    discord_webhook_url: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_to: str | None = None
    dry_run_send_alerts: bool = False
