"""``engine_base_url(tenant)``: one engine per tenant, found three ways.

A child tenant's engine is its own deployment, and the bridge has to dial it
rather than the root engine — which refuses a token minted for another tenant
anyway (``engine/auth.py``). Resolution order for a non-root tenant:

1. ``GENUS_TENANT_ENGINE_URLS`` — an explicit ``{tenant: url}`` map. Local
   development needs it: every engine is 127.0.0.1 on a different port.
2. ``GENUS_TENANT_ENGINE_URL_TEMPLATE`` — a URL with a literal ``{tenant}``.
3. The single engine URL, with ONE warning per process.

The root tenant (and ``None``) always gets the single URL.
"""

from __future__ import annotations

import json
import logging

import pytest

from robothor import engine_control

ROOT = "acme-parent"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "GENUS_TENANT_ENGINE_URLS",
        "ROBOTHOR_TENANT_ENGINE_URLS",
        "GENUS_TENANT_ENGINE_URL_TEMPLATE",
        "ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_URL", "http://127.0.0.1:18800")
    monkeypatch.setenv("ROBOTHOR_PLATFORM_TENANT", ROOT)
    from robothor.settings import reset_settings

    reset_settings()
    engine_control.reset_tenant_url_warning()
    yield
    reset_settings()
    engine_control.reset_tenant_url_warning()


def _reset():
    from robothor.settings import reset_settings

    reset_settings()


def test_no_tenant_is_the_single_url():
    assert engine_control.engine_base_url() == "http://127.0.0.1:18800"


def test_the_root_tenant_is_the_single_url(monkeypatch):
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://engine-{tenant}:18800")
    _reset()
    assert engine_control.engine_base_url(ROOT) == "http://127.0.0.1:18800"


def test_the_explicit_map_wins(monkeypatch):
    monkeypatch.setenv(
        "GENUS_TENANT_ENGINE_URLS", json.dumps({"acme-child-a": "http://127.0.0.1:18801/"})
    )
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://engine-{tenant}:18800")
    _reset()
    assert engine_control.engine_base_url("acme-child-a") == "http://127.0.0.1:18801"
    # Not in the map: the template.
    assert engine_control.engine_base_url("acme-child-b") == "http://engine-acme-child-b:18800"


def test_the_robothor_alias_is_honoured(monkeypatch):
    monkeypatch.setenv(
        "ROBOTHOR_TENANT_ENGINE_URLS", json.dumps({"acme-child-a": "http://127.0.0.1:18801"})
    )
    _reset()
    assert engine_control.engine_base_url("acme-child-a") == "http://127.0.0.1:18801"


def test_the_template_fills_the_tenant(monkeypatch):
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://genus-os-engine-{tenant}:18800")
    _reset()
    assert (
        engine_control.engine_base_url("acme-child-a")
        == "http://genus-os-engine-acme-child-a:18800"
    )


def test_a_bad_scheme_in_the_map_is_refused(monkeypatch):
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URLS", json.dumps({"acme-child-a": "file:///etc"}))
    _reset()
    with pytest.raises(ValueError, match="GENUS_TENANT_ENGINE_URLS"):
        engine_control.engine_base_url("acme-child-a")


def test_a_map_that_is_not_a_json_object_is_refused(monkeypatch):
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URLS", "[1, 2]")
    _reset()
    with pytest.raises(ValueError, match="GENUS_TENANT_ENGINE_URLS"):
        engine_control.engine_base_url("acme-child-a")


def test_a_tenant_id_that_would_change_the_host_shape_is_refused(monkeypatch):
    monkeypatch.setenv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://engine-{tenant}:18800")
    _reset()
    with pytest.raises(ValueError):
        engine_control.engine_base_url("evil.example.com/x?")


def test_with_neither_it_falls_back_once_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="robothor.engine_control"):
        assert engine_control.engine_base_url("acme-child-a") == "http://127.0.0.1:18800"
        assert engine_control.engine_base_url("acme-child-b") == "http://127.0.0.1:18800"
    warnings = [r for r in caplog.records if "GENUS_TENANT_ENGINE_URL" in r.getMessage()]
    assert len(warnings) == 1


def test_the_settings_are_declared_with_their_aliases():
    from robothor.settings.registry import declared_env_names

    names = declared_env_names()
    for name in (
        "GENUS_TENANT_ENGINE_URLS",
        "ROBOTHOR_TENANT_ENGINE_URLS",
        "GENUS_TENANT_ENGINE_URL_TEMPLATE",
        "ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE",
    ):
        assert name in names
