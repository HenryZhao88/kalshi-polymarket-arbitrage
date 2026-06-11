"""Settings: safe defaults and secret masking."""

from arb_scanner.app.config import Mode, Settings


def test_default_mode_is_discovery_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.mode is Mode.DISCOVERY_ONLY


def test_secrets_are_masked_in_repr(monkeypatch: object) -> None:
    settings = Settings(_env_file=None, kalshi_api_key_id="super-secret-key")  # type: ignore[arg-type]
    assert "super-secret-key" not in repr(settings)
    assert settings.kalshi_api_key_id is not None
    assert settings.kalshi_api_key_id.get_secret_value() == "super-secret-key"
