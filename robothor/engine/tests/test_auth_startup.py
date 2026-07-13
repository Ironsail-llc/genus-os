"""Engine startup authentication configuration gates."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.auth.runtime import AuthConfigurationError


def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_BRIDGE_SSO_SECRET", raising=False)
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)


def test_engine_accepts_signing_key_without_bridge_sso_secrets(monkeypatch) -> None:
    _production(monkeypatch)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-signing-key-that-is-at-least-32-bytes")

    from robothor.engine.health import validate_engine_auth_configuration

    validate_engine_auth_configuration()


def test_engine_rejects_missing_production_signing_key(monkeypatch) -> None:
    _production(monkeypatch)
    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)

    from robothor.engine.health import validate_engine_auth_configuration

    with pytest.raises(AuthConfigurationError, match="SIGNING_KEY"):
        validate_engine_auth_configuration()


@pytest.mark.asyncio
async def test_daemon_rejects_unsafe_auth_before_startup_side_effects(monkeypatch) -> None:
    _production(monkeypatch)
    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)

    from robothor.engine import daemon

    with (
        patch.object(daemon, "_set_daemon_start_ts") as set_start,
        patch.object(daemon, "_cleanup_stale_runs") as cleanup,
        pytest.raises(AuthConfigurationError, match="SIGNING_KEY"),
    ):
        await daemon.main()

    set_start.assert_not_called()
    cleanup.assert_not_called()
