"""Application settings via pydantic-settings.

All values come from environment variables prefixed `ARB_` (or a local `.env`).
Secrets are `SecretStr` so they never appear in logs or reprs. Thresholds and
risk limits are added here, never hardcoded at call sites.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import SecretStr
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
