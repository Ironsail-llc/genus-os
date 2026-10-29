"""The integrations settings group: n8n's four settings, opt-in and secret-safe."""

from __future__ import annotations

from robothor.settings.model import GenusSettings


def _field(name: str):
    from robothor.settings.model import IntegrationSettings

    return IntegrationSettings.model_fields[name]


def test_the_group_is_wired_into_the_root_settings() -> None:
    assert "integrations" in GenusSettings.model_fields
    from robothor.settings.model import IntegrationSettings

    group = IntegrationSettings()
    assert group.n8n_enabled is False
    assert group.n8n_base_url == ""
    assert group.n8n_api_key == ""
    assert group.n8n_poll_seconds == 120


def test_env_names_and_flags() -> None:
    expected = {
        "n8n_enabled": ("ROBOTHOR_N8N_ENABLED", False, True),
        "n8n_base_url": ("ROBOTHOR_N8N_BASE_URL", False, True),
        "n8n_api_key": ("ROBOTHOR_N8N_API_KEY", True, True),
        "n8n_poll_seconds": ("ROBOTHOR_N8N_POLL_SECONDS", False, False),
    }
    for name, (env, secret, restart) in expected.items():
        extra = _field(name).json_schema_extra
        assert extra["env"] == env
        assert extra["secret"] is secret
        assert extra["restart_required"] is restart
        assert extra["since"] == "unreleased"
        assert extra["governed"] is False


def test_the_environment_reaches_the_group(monkeypatch) -> None:
    from robothor.settings import get_settings, reset_settings

    monkeypatch.setenv("ROBOTHOR_N8N_ENABLED", "true")
    monkeypatch.setenv("ROBOTHOR_N8N_BASE_URL", "https://n8n.example.com")
    monkeypatch.setenv("ROBOTHOR_N8N_POLL_SECONDS", "300")
    reset_settings()
    try:
        group = get_settings().integrations
        assert group.n8n_enabled is True
        assert group.n8n_base_url == "https://n8n.example.com"
        assert group.n8n_poll_seconds == 300
    finally:
        reset_settings()
