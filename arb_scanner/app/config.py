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

    # --- Execution gates (all fail closed) -------------------------------
    # Placing a real order requires EVERY one of these to be satisfied:
    #   1. mode == EXECUTION_ENABLED          (above)
    #   2. live_order_placement == True       (this explicit second switch)
    #   3. a passing runtime geoblock check   (clients/geoblock.py, call-time)
    #   4. the kill switch is clear           (risk/kill_switch.py)
    #   5. per-order notional <= the cap below
    #   6. a successful balance preflight unless require_balance_preflight off
    # Missing any one of these means no order is placed. There is intentionally
    # no single flag that enables trading on its own.
    live_order_placement: bool = False
    #: When True (default) the executor logs the orders it WOULD place and the
    #: gate decisions, but never calls a venue order endpoint. Set False only
    #: after live_order_placement and the geoblock check both pass for you.
    execution_dry_run: bool = True
    #: Hard cap on a single leg's notional (price × size) in dollars. The
    #: executor refuses any leg above this regardless of other limits.
    max_order_notional_dollars: Decimal = Field(default=Decimal("100"), ge=0)
    #: Refuse to trade unless a balance check on both venues confirms funds to
    #: cover the locked capital first.
    require_balance_preflight: bool = True
    #: Limit-order price padding (in probability, 0–1) added to the taker VWAP
    #: so a marketable limit crosses without paying through the whole book.
    execution_limit_price_pad: Decimal = Field(default=Decimal("0.01"), ge=0, le=Decimal("0.5"))

    database_url: str = "sqlite+aiosqlite:///arb_scanner.db"
    persist_scans: bool = True
    persist_raw_candidates: bool = False
    storage_retention_days: int = Field(default=30, ge=1, le=3650)
    storage_max_candidates_per_scan: int = Field(default=5000, ge=1, le=1_000_000)
    kill_switch_file: Path | None = Path(".arb-scanner.kill")
    # Full-venue coverage by default (operator decision 2026-06-21). The keyset
    # and REST pagination loops stop early when a venue is exhausted, so these
    # high caps are safety guardrails, not a fixed fetch size. Lower them to
    # sample a slice of the universe per pass.
    polymarket_max_markets: int = Field(default=50_000, ge=101, le=200_000)
    polymarket_page_size: int = Field(default=100, ge=1, le=100)
    polymarket_max_pages: int = Field(default=500, ge=1, le=2_000)
    kalshi_page_limit: int = Field(default=1000, ge=1, le=1000)
    kalshi_max_pages: int = Field(default=200, ge=1, le=2_000)

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
