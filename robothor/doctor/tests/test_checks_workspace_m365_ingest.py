"""workspace.m365_ingest_freshness: the Microsoft 365 ingest is keeping up."""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import workspace_m365
from robothor.doctor.tests.conftest import fake_db, make_ctx

CHECK = next(c for c in workspace_m365.CHECKS if c.id == "workspace.m365_ingest_freshness")


@pytest.fixture
def settings(monkeypatch):
    def _set(**pairs: str) -> None:
        from robothor.settings import reset_settings

        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


@pytest.fixture
def m365(settings):
    settings(
        ROBOTHOR_WORKSPACE_PROVIDER="microsoft365",
        ROBOTHOR_M365_ASSISTANT_MAILBOX="assistant@example.com",
        ROBOTHOR_M365_OWNER_MAILBOX="owner@example.com",
        ROBOTHOR_M365_INGEST_INTERVAL_SECONDS="60",
    )


def _run(rows=None, *, error=None):
    db = fake_db(rows, error=error)
    result = asyncio.run(CHECK.run(make_ctx(db_factory=db)))
    return (result[0] if isinstance(result, list) else result), db


def test_it_is_recommended_and_registered() -> None:
    from robothor.doctor.registry import builtin_checks

    assert CHECK.severity == "recommended"
    assert "workspace.m365_ingest_freshness" in {c.id for c in builtin_checks()}


def test_a_google_instance_skips_without_reading_the_database(monkeypatch) -> None:
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: True)
    result, db = _run([("mail", 1.0)])
    assert result.status == "skip"
    assert "google" in result.detail
    assert db.connection.cursors == []


def test_fresh_mail_and_calendar_pass(m365) -> None:
    result, db = _run([("mail", 30.0), ("calendar", 100.0)])
    assert result.status == "pass", result.detail
    sql, params = db.connection.cursors[0].executed[0]
    assert "workspace_sync_state" in sql and "microsoft365" in params


def test_a_stale_resource_fails_and_names_it(m365) -> None:
    result, _db = _run([("mail", 30.0), ("calendar", 181.0)])
    assert result.status == "fail"
    assert "calendar" in result.detail and "180" in result.detail


def test_a_resource_that_never_synced_fails(m365) -> None:
    result, _db = _run([("mail", 30.0)])
    assert result.status == "fail"
    assert "calendar" in result.detail and "never" in result.detail


def test_without_an_owner_mailbox_only_mail_is_expected(settings) -> None:
    settings(
        ROBOTHOR_WORKSPACE_PROVIDER="microsoft365",
        ROBOTHOR_M365_ASSISTANT_MAILBOX="assistant@example.com",
    )
    result, _db = _run([("mail", 10.0)])
    assert result.status == "pass", result.detail


def test_an_unreadable_state_table_fails_with_the_error_type(m365) -> None:
    result, _db = _run(error=RuntimeError("no database"))
    assert result.status == "fail"
    assert "RuntimeError" in result.detail
