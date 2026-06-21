"""Settings: safe defaults and secret masking."""

from pydantic import SecretStr

from arb_scanner.app.config import Mode, Settings


def test_default_mode_is_discovery_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.mode is Mode.DISCOVERY_ONLY


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(_env_file=None, kalshi_api_key_id=SecretStr("super-secret-key"))
    assert "super-secret-key" not in repr(settings)
    assert settings.kalshi_api_key_id is not None
    assert settings.kalshi_api_key_id.get_secret_value() == "super-secret-key"


def test_dry_run_external_alerts_default_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.dry_run_send_alerts is False
    assert settings.persist_scans is True
    assert settings.persist_raw_candidates is False
    assert settings.storage_retention_days == 30
    assert settings.storage_max_candidates_per_scan == 5000


def test_pagination_defaults_cover_the_full_active_universe() -> None:
    # Default to full-venue coverage (operator decision 2026-06-21): the
    # keyset/REST loops stop early when a venue is exhausted, so high caps are
    # safe guardrails rather than a fixed fetch size.
    settings = Settings(_env_file=None)
    assert settings.polymarket_page_size == 100
    assert settings.polymarket_max_markets == 50_000
    assert settings.polymarket_max_pages == 500
    # page_size * max_pages must be able to reach max_markets in one pass.
    assert settings.polymarket_page_size * settings.polymarket_max_pages >= (
        settings.polymarket_max_markets
    )
    assert settings.kalshi_page_limit == 1000
    assert settings.kalshi_max_pages == 200
