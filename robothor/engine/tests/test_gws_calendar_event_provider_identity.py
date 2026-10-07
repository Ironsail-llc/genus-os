"""calendar_event rows are keyed by (tenant, provider, external id), not Google's id.

Migration 148 adds ``provider`` + ``external_event_id`` so a Microsoft 365
event can live next to a Google one without a ``google_event_id`` it does not
have. ``_record_calendar_event`` must:

* write ``provider`` and ``external_event_id`` on every upsert;
* keep writing ``google_event_id`` for Google events (dual-write, so readers
  and the old unique constraint keep working);
* upsert on the new ``(tenant_id, provider, external_event_id)`` key;
* default ``provider`` to ``'google'`` so the existing caller is unchanged.

No database: a fake connection records the SQL and parameters.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws

EVENT = {
    "id": "gcal-evt-42",
    "summary": "Planning",
    "status": "confirmed",
    "start": {"dateTime": "2026-05-01T15:00:00Z"},
    "end": {"dateTime": "2026-05-01T15:30:00Z"},
    "attendees": [],
}


class _Cursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]):
        self.calls = calls

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, tuple(params)))

    def fetchone(self) -> tuple[str]:
        return ("row-id",)


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)

    def commit(self) -> None:
        pass


@pytest.fixture
def conn(monkeypatch):
    fake = _Conn()

    @contextmanager
    def _get_connection():
        yield fake

    monkeypatch.setattr("robothor.db.connection.get_connection", _get_connection)
    monkeypatch.setattr(gws, "_resolve_person_by_email", lambda _email: None)
    return fake


def _insert(conn: _Conn) -> tuple[str, dict[str, Any]]:
    sql, params = next(c for c in conn.calls if "INSERT INTO calendar_event\n" in c[0])
    cols_match = re.search(r"INSERT INTO calendar_event\s*\(([^)]*)\)", sql)
    assert cols_match, sql
    cols = [c.strip() for c in cols_match.group(1).split(",")]
    assert len(cols) == len(params), (cols, params)
    return sql, dict(zip(cols, params, strict=True))


def test_google_event_writes_provider_and_external_id_and_keeps_google_id(conn):
    gws._record_calendar_event(result=dict(EVENT), tenant_id="t1")

    _sql, row = _insert(conn)
    assert row["provider"] == "google"
    assert row["external_event_id"] == "gcal-evt-42"
    assert row["google_event_id"] == "gcal-evt-42"
    assert row["tenant_id"] == "t1"


def test_upsert_targets_the_provider_neutral_key(conn):
    gws._record_calendar_event(result=dict(EVENT), tenant_id="t1")

    sql, _row = _insert(conn)
    normalized = " ".join(sql.split())
    assert "ON CONFLICT (tenant_id, provider, external_event_id)" in normalized
    assert "ON CONFLICT (tenant_id, google_event_id)" not in normalized
    # Same update semantics as before: the event's fields are refreshed.
    for col in (
        "title",
        "description",
        "location",
        "start_at",
        "end_at",
        "organizer_email",
        "hangout_link",
        "status",
    ):
        assert f"{col} = EXCLUDED.{col}" in normalized


def test_non_google_provider_does_not_claim_a_google_event_id(conn):
    gws._record_calendar_event(result=dict(EVENT, id="AAMkAD-ms-1"), provider="microsoft")

    _sql, row = _insert(conn)
    assert row["provider"] == "microsoft"
    assert row["external_event_id"] == "AAMkAD-ms-1"
    assert row["google_event_id"] is None
