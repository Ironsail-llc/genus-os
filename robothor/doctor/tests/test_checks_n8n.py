"""n8n.*: the n8n monitor is configured, reachable, and polling."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from robothor.doctor.checks import n8n as n8n_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx
from robothor.integrations.n8n.client import N8nClient

BASE = "https://n8n.example.com"
KEY = "n8n-api-key-0123456789abcdef"


def _check(check_id: str):
    return next(c for c in n8n_checks.CHECKS if c.id == check_id)


def _run(check_id: str, ctx=None):
    result = asyncio.run(_check(check_id).run(ctx or make_ctx()))
    return result[0] if isinstance(result, list) else result


@pytest.fixture
def settings(monkeypatch):
    def _set(**pairs: str) -> None:
        from robothor.settings import reset_settings

        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


@pytest.fixture
def enabled(settings, monkeypatch):
    settings(
        ROBOTHOR_N8N_ENABLED="true",
        ROBOTHOR_N8N_BASE_URL=BASE,
        ROBOTHOR_N8N_POLL_SECONDS="120",
    )
    monkeypatch.setattr(n8n_checks, "_key_source", lambda _tenant: "vault")


def test_the_checks_are_registered_and_recommended() -> None:
    from robothor.doctor.registry import builtin_checks

    ids = {c.id for c in builtin_checks()}
    for check_id in ("n8n.configured", "n8n.connection", "n8n.poll_freshness"):
        assert check_id in ids
        assert _check(check_id).severity == "recommended"
        assert (_check(check_id).run.__doc__ or "").strip()


@pytest.mark.parametrize("check_id", ["n8n.configured", "n8n.connection", "n8n.poll_freshness"])
def test_every_check_skips_when_the_monitor_is_off(check_id, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("a disabled monitor must not be probed")

    monkeypatch.setattr(n8n_checks, "_key_source", boom)
    monkeypatch.setattr(n8n_checks, "_make_client", boom)
    db = fake_db([(1.0, None)])
    result = _run(check_id, make_ctx(db_factory=db))
    assert result.status == "skip"
    assert "ROBOTHOR_N8N_ENABLED" in result.detail
    assert db.connection.cursors == []


def test_configured_passes_with_a_base_url_and_a_key(enabled) -> None:
    result = _run("n8n.configured")
    assert result.status == "pass", result.detail
    assert BASE in result.detail and "vault" in result.detail


def test_configured_fails_without_a_base_url(settings, monkeypatch) -> None:
    settings(ROBOTHOR_N8N_ENABLED="true")
    monkeypatch.setattr(n8n_checks, "_key_source", lambda _tenant: "vault")
    result = _run("n8n.configured")
    assert result.status == "fail"
    assert "ROBOTHOR_N8N_BASE_URL" in result.detail


def test_configured_fails_without_a_key(enabled, monkeypatch) -> None:
    monkeypatch.setattr(n8n_checks, "_key_source", lambda _tenant: "missing")
    result = _run("n8n.configured")
    assert result.status == "fail"
    assert "ROBOTHOR_N8N_API_KEY" in result.detail


def _client_for(handler):
    return lambda _settings: N8nClient(BASE, KEY, transport=httpx.MockTransport(handler))


def test_connection_passes_on_healthz_and_one_workflow(enabled, monkeypatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": [{"id": "wf1"}], "nextCursor": "x"})

    monkeypatch.setattr(n8n_checks, "_make_client", _client_for(handler))
    result = _run("n8n.connection")
    assert result.status == "pass", result.detail
    assert paths == ["/healthz?", "/api/v1/workflows?limit=1"]


def test_connection_fails_by_class_never_by_key(enabled, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401, json={"message": f"invalid key {KEY}"})

    monkeypatch.setattr(n8n_checks, "_make_client", _client_for(handler))
    result = _run("n8n.connection")
    assert result.status == "fail"
    assert "auth" in result.detail
    assert KEY not in result.detail


def test_connection_skips_offline(enabled, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("--offline must not leave the box")

    monkeypatch.setattr(n8n_checks, "_make_client", boom)
    result = _run("n8n.connection", make_ctx(offline=True))
    assert result.status == "skip"
    assert "offline" in result.detail


def test_freshness_passes_within_three_intervals(enabled) -> None:
    db = fake_db([(100.0, None)])
    result = _run("n8n.poll_freshness", make_ctx(db_factory=db))
    assert result.status == "pass", result.detail
    sql, params = db.connection.cursors[0].executed[0]
    assert "n8n_sync_state" in sql


def test_freshness_fails_when_stale_and_names_the_last_error(enabled) -> None:
    db = fake_db([(361.0, "auth (HTTP 401)")])
    result = _run("n8n.poll_freshness", make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "360" in result.detail and "auth (HTTP 401)" in result.detail


def test_freshness_fails_when_never_polled(enabled) -> None:
    db = fake_db([(None, "network")])
    result = _run("n8n.poll_freshness", make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "never" in result.detail


def test_freshness_fails_when_there_is_no_row(enabled) -> None:
    db = fake_db([])
    result = _run("n8n.poll_freshness", make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "never" in result.detail


def test_freshness_reports_an_unreadable_table(enabled) -> None:
    db = fake_db(error=RuntimeError("no table"))
    result = _run("n8n.poll_freshness", make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "RuntimeError" in result.detail
