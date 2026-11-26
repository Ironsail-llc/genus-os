"""The four Datadog settings: declared, secret where they are credentials."""

from __future__ import annotations


def _field(name: str):
    from robothor.settings.model import IntegrationSettings

    return IntegrationSettings.model_fields[name]


def test_defaults() -> None:
    from robothor.settings.model import IntegrationSettings

    group = IntegrationSettings()
    assert group.datadog_api_key == ""
    assert group.datadog_app_key == ""
    assert group.datadog_site == "datadoghq.com"
    assert group.datadog_scopes == ""


def test_env_names_and_flags() -> None:
    expected = {
        "datadog_api_key": ("ROBOTHOR_DATADOG_API_KEY", True),
        "datadog_app_key": ("ROBOTHOR_DATADOG_APP_KEY", True),
        "datadog_site": ("ROBOTHOR_DATADOG_SITE", False),
        "datadog_scopes": ("ROBOTHOR_DATADOG_SCOPES", False),
    }
    for name, (env, secret) in expected.items():
        extra = _field(name).json_schema_extra
        assert extra["env"] == env
        assert extra["secret"] is secret
        assert extra["since"] == "unreleased"


def test_the_environment_reaches_the_group(monkeypatch) -> None:
    from robothor.settings import get_settings, reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SITE", "datadoghq.eu")
    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", '{"acme-child-a": {"logs": "service:shop-api"}}')
    reset_settings()
    try:
        group = get_settings().integrations
        assert group.datadog_site == "datadoghq.eu"
        assert "acme-child-a" in group.datadog_scopes
    finally:
        reset_settings()
