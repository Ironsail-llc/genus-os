"""datadog.reachable: the shared read-only Datadog keys work, and which tenants have no scope."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from robothor.doctor.checks import datadog as dd_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx
from robothor.integrations.datadog.client import DatadogClient

API_KEY = "dd-api-key-0123456789abcdef"
APP_KEY = "dd-app-key-fedcba9876543210"


def _check():
    return next(c for c in dd_checks.CHECKS if c.id == "datadog.reachable")


def _run(ctx=None):
    result = asyncio.run(_check().run(ctx or make_ctx()))
    return result[0] if isinstance(result, list) else result


@pytest.fixture
def settings(monkeypatch):
    from robothor.settings import reset_settings

    def _set(**pairs: str) -> None:
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(dd_checks, "_keys_set", lambda _settings: True)


def _answer(status: int, body: object = None, *, app_key_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/logs/events/search":
            return httpx.Response(app_key_status, json={"data": []})
        assert request.url.path == "/api/v1/validate"
        return httpx.Response(status, json=body if body is not None else {})

    return lambda _settings: DatadogClient(API_KEY, APP_KEY, transport=httpx.MockTransport(handler))


def test_the_check_is_registered() -> None:
    from robothor.doctor.registry import builtin_checks

    assert "datadog.reachable" in {c.id for c in builtin_checks()}
    assert _check().severity == "recommended"
    assert (_check().run.__doc__ or "").strip()


def test_not_configured_is_a_skip_not_a_failure(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("an unconfigured integration must not be called")

    monkeypatch.setattr(dd_checks, "_keys_set", lambda _settings: False)
    monkeypatch.setattr(dd_checks, "_make_client", boom)
    result = _run()
    assert result.status == "skip"
    assert "not configured" in result.detail
    assert "ROBOTHOR_DATADOG_API_KEY" in result.detail


def test_the_real_key_lookup_sees_no_keys_on_a_bare_instance(monkeypatch) -> None:
    monkeypatch.setattr(
        "robothor.integrations.datadog.client._get_secret", lambda _name, _tenant: None
    )
    assert _run().status == "skip"


def test_offline_skips(keys, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("--offline must not leave the box")

    monkeypatch.setattr(dd_checks, "_make_client", boom)
    result = _run(make_ctx(offline=True))
    assert result.status == "skip" and "offline" in result.detail


def test_passes_when_the_keys_validate(keys, monkeypatch) -> None:
    monkeypatch.setattr(dd_checks, "_make_client", _answer(200, {"valid": True}))
    result = _run(make_ctx(db_factory=fake_db([])))
    assert result.status == "pass", result.detail


def test_a_rejected_key_fails_without_printing_it(keys, monkeypatch) -> None:
    monkeypatch.setattr(dd_checks, "_make_client", _answer(403, {"errors": [API_KEY]}))
    result = _run()
    assert result.status == "fail"
    assert "rejected" in result.detail
    assert API_KEY not in result.detail and APP_KEY not in result.detail


def test_a_rejected_application_key_fails_even_when_the_api_key_is_valid(keys, monkeypatch) -> None:
    monkeypatch.setattr(
        dd_checks, "_make_client", _answer(200, {"valid": True}, app_key_status=403)
    )
    result = _run()
    assert result.status == "fail"
    assert "application key" in result.detail
    assert API_KEY not in result.detail and APP_KEY not in result.detail


def test_an_unreachable_datadog_fails_by_kind(keys, monkeypatch) -> None:
    monkeypatch.setattr(dd_checks, "_make_client", _answer(503))
    result = _run()
    assert result.status == "fail" and "network" in result.detail


def test_an_unknown_site_fails_with_a_plain_sentence(keys, settings) -> None:
    settings(ROBOTHOR_DATADOG_SITE="logs.example.com")
    result = _run()
    assert result.status == "fail"
    assert "ROBOTHOR_DATADOG_SITE" in result.detail


def test_active_tenants_without_a_scope_are_named(keys, settings, monkeypatch) -> None:
    settings(ROBOTHOR_DATADOG_SCOPES=json.dumps({"acme-child-a": {"logs": "service:shop-api"}}))
    monkeypatch.setattr(dd_checks, "_make_client", _answer(200, {"valid": True}))
    db = fake_db([("acme-child-a",), ("acme-child-b",)])
    result = _run(make_ctx(db_factory=db))
    assert result.status == "pass"
    assert "acme-child-b" in result.detail
    assert "acme-child-a" not in result.detail
    sql, _params = db.connection.cursors[0].executed[0]
    assert "crm_tenants" in sql and "active" in sql


def test_an_unreadable_tenant_list_does_not_fail_the_check(keys, monkeypatch) -> None:
    monkeypatch.setattr(dd_checks, "_make_client", _answer(200, {"valid": True}))
    result = _run(make_ctx(db_factory=fake_db(error=RuntimeError("down"))))
    assert result.status == "pass"
    assert "RuntimeError" in result.detail
