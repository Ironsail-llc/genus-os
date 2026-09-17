"""A control's evidence row must survive an instance that has not migrated.

``agent_guardrail_events.action`` is an enumerated CHECK, extended once before
(migration 079 added ``observed``) and again by 124 for ``context_overflow``.
Every write here is best-effort and swallowed at DEBUG, which is right for a
telemetry path and is also how a control becomes inert without anyone noticing:
between a deploy and ``genus migrate`` the new action would be refused by the
old constraint, the row would vanish, and the evidence table would report the
control had never fired.

So the writer degrades on purpose: the row lands as ``warned`` — a value every
instance has allowed since 001 — and the operator is told once, by name, which
migration to run. A dropped row is the one outcome that is not acceptable.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robothor.engine import tracking


class _CheckViolationError(Exception):
    """Stands in for ``psycopg2.errors.CheckViolation``.

    Only its SQLSTATE matters: the writer matches on ``pgcode``, not on a
    driver's exception class, so this test needs no PostgreSQL error hierarchy
    to prove the degrade path runs.
    """

    pgcode = "23514"


class _Cursor:
    def __init__(self, refuse: set[str]) -> None:
        self.refuse = refuse
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        action = params[3] if params and len(params) > 3 else None
        if action in self.refuse:
            raise _CheckViolationError(
                'new row for relation "agent_guardrail_events" violates check '
                'constraint "agent_guardrail_events_action_check"'
            )
        self.executed.append((sql, params))


class _Connection:
    def __init__(self, refuse: set[str]) -> None:
        self.cursors: list[_Cursor] = []
        self.refuse = refuse

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        cursor = _Cursor(self.refuse)
        self.cursors.append(cursor)
        return cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


@pytest.fixture
def db(monkeypatch):
    """A connection factory whose CHECK refuses whatever the test names."""

    def _factory(refuse: set[str]):
        connections: list[_Connection] = []

        def _get_connection():
            connection = _Connection(refuse)
            connections.append(connection)
            return connection

        monkeypatch.setattr(tracking, "get_connection", _get_connection)
        tracking.reset_guardrail_action_warning()
        return connections

    return _factory


def _actions(connections) -> list[str]:
    return [
        params[3]
        for connection in connections
        for cursor in connection.cursors
        for _sql, params in cursor.executed
    ]


class TestAMigratedInstance:
    def test_the_literal_action_is_written(self, db):
        connections = db(refuse=set())

        tracking.log_guardrail_event("run-1", "context_overflow", "context_overflow")

        assert _actions(connections) == ["context_overflow"]


class TestAnInstanceThatHasNotMigrated:
    def test_the_row_still_lands_as_warned(self, db):
        connections = db(refuse={"context_overflow"})

        tracking.log_guardrail_event("run-1", "context_overflow", "context_overflow")

        assert _actions(connections) == ["warned"], "the evidence row was dropped"

    def test_the_guardrail_name_is_unchanged_so_evidence_still_finds_it(self, db):
        connections = db(refuse={"context_overflow"})

        tracking.log_guardrail_event("run-1", "context_overflow", "context_overflow")

        names = [
            params[2]
            for connection in connections
            for cursor in connection.cursors
            for _sql, params in cursor.executed
        ]
        assert names == ["context_overflow"]

    def test_the_operator_is_told_once_which_migration_to_run(self, db, caplog):
        db(refuse={"context_overflow"})

        with caplog.at_level(logging.WARNING, logger=tracking.__name__):
            for _ in range(5):
                tracking.log_guardrail_event("run-1", "context_overflow", "context_overflow")

        lines = [r for r in caplog.records if "genus migrate" in r.message]
        assert len(lines) == 1, "one line per process, not one per event"
        assert "124" in lines[0].message

    def test_a_legacy_action_is_never_retried(self, db):
        """`warned` failing is a different fault, and looping on it helps nobody."""
        connections = db(refuse={"warned"})

        tracking.log_guardrail_event("run-1", "rbac", "warned")

        assert _actions(connections) == []


class TestEverythingElseIsStillSwallowed:
    def test_an_unreachable_database_does_not_raise(self, monkeypatch):
        def _boom():
            raise RuntimeError("no database here")

        monkeypatch.setattr(tracking, "get_connection", _boom)
        tracking.log_guardrail_event("run-1", "context_overflow", "context_overflow")
