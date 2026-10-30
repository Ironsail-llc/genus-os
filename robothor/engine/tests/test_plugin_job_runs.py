"""A scheduled plugin job leaves a run record, like a workflow does.

`register_plugin_jobs` used to hand a plugin's callable straight to
APScheduler: no listener, no row. A job that bypassed workflows ran
invisibly, so the console could not say whether it ran, how long it took,
what it returned or why it failed.

Each execution now writes a ``workflow_runs`` row keyed by the job id
(``plugin:<name>``) with ``trigger_type = 'plugin_job'``: it is the table the
run listings already read, so the job shows up next to the workflows with no
new route. These tests drive the job the way APScheduler does: they take the
callable it was given and call it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from robothor.plugins import reload_plugins


class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((" ".join(sql.split()), params))

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    def cursor(self, *a: Any, **kw: Any) -> _FakeCursor:
        return _FakeCursor(self._log)

    def commit(self) -> None:
        pass

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def db(monkeypatch):
    """Every statement the job's recording sends, in order."""
    log: list[tuple[str, tuple]] = []
    from robothor.db import connection

    monkeypatch.setattr(connection, "get_connection", lambda *a, **kw: _FakeConn(log))
    return log


class _JobEP:
    group = "genus.jobs"
    name = "testjobs"

    def __init__(self, jobs: dict):
        self._jobs = jobs

    def load(self):
        return {"genus_contract_version": "1.0", "jobs": self._jobs}


@pytest.fixture
def scheduled(monkeypatch):
    """Install plugin jobs, register them, return the callable APScheduler holds."""

    def _scheduled(name: str, func):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from robothor.engine.scheduler import CronScheduler
        from robothor.plugins import loader

        monkeypatch.setattr(
            loader, "_discover", lambda: [_JobEP({name: {"cron": "0 * * * *", "func": func}})]
        )
        reload_plugins()
        s = CronScheduler.__new__(CronScheduler)
        s.scheduler = AsyncIOScheduler()
        assert s.register_plugin_jobs() == 1
        return s.scheduler.get_job(f"plugin:{name}").func

    yield _scheduled
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


def _inserts(db):
    return [(sql, p) for sql, p in db if sql.startswith("INSERT INTO workflow_runs")]


def _updates(db):
    return [(sql, p) for sql, p in db if sql.startswith("UPDATE workflow_runs")]


def _row(db) -> dict[str, Any]:
    """Fold the INSERT and the final UPDATE into one row, by column name."""
    row: dict[str, Any] = {}
    for sql, params in _inserts(db) + _updates(db):
        if sql.startswith("INSERT"):
            cols = sql.split("(", 1)[1].split(")", 1)[0]
            names = [c.strip() for c in cols.split(",")]
        else:
            sets = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            names = [c.split("=")[0].strip() for c in sets.split(",")]
        row.update(dict(zip(names, params, strict=False)))
    return row


class TestASuccessfulRunIsRecorded:
    """AC1: a run row exists with its status and return value."""

    def test_the_job_writes_a_workflow_run_row_keyed_by_its_id(self, scheduled, db):
        func = scheduled("sweep", lambda: {"processed": 3})
        func()
        row = _row(db)
        assert row["workflow_id"] == "plugin:sweep"
        assert row["trigger_type"] == "plugin_job"

    def test_the_row_ends_completed_with_a_duration(self, scheduled, db):
        func = scheduled("sweep", lambda: {"processed": 3})
        func()
        row = _row(db)
        assert row["status"] == "completed"
        assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0
        assert row["completed_at"] is not None

    def test_it_is_running_while_the_job_runs(self, scheduled, db):
        seen: list[str] = []

        def job():
            seen.append(_row(db).get("status", "no row"))

        func = scheduled("sweep", job)
        func()
        assert _row(db)["status"] == "completed"
        assert seen == ["running"], "the row must exist, running, before the job starts"

    def test_the_returned_dict_is_stored_on_the_row(self, scheduled, db):
        func = scheduled("sweep", lambda: {"processed": 3, "skipped": 1})
        func()
        context = json.loads(_row(db)["context"])
        assert context["result"] == {"processed": 3, "skipped": 1}

    def test_the_job_still_returns_its_value(self, scheduled, db):
        func = scheduled("sweep", lambda: {"processed": 3})
        assert func() == {"processed": 3}


class TestAFailureIsRecorded:
    """AC1: an exception marks the run failed."""

    def test_an_exception_marks_the_row_failed_with_the_error(self, scheduled, db):
        def job():
            raise RuntimeError("upstream refused the write")

        func = scheduled("sweep", job)
        with pytest.raises(RuntimeError):
            func()
        row = _row(db)
        assert row["status"] == "failed"
        assert "upstream refused the write" in row["error_message"]
        assert "RuntimeError" in row["error_message"]
        assert isinstance(row["duration_ms"], int)


class TestTheReturnValueIsCapped:
    def test_a_huge_return_value_is_truncated_not_stored_whole(self, scheduled, db):
        from robothor.engine.plugin_job_runs import RESULT_CAP_CHARS

        func = scheduled("sweep", lambda: {"blob": "x" * (RESULT_CAP_CHARS * 4)})
        func()
        stored = _row(db)["context"]
        assert len(stored) < RESULT_CAP_CHARS * 2
        context = json.loads(stored)
        assert context["result_truncated"] is True

    def test_a_value_json_cannot_encode_is_stored_as_text(self, scheduled, db):
        marker = object()
        func = scheduled("sweep", lambda: {"thing": marker})
        func()
        context = json.loads(_row(db)["context"])
        assert "object" in str(context["result"])


class TestAsyncJobs:
    def test_a_coroutine_job_stays_a_coroutine_function(self, scheduled, db):
        """AsyncIOScheduler only awaits coroutine functions; a sync wrapper
        around one would return an un-awaited coroutine and never run it."""

        async def job():
            return {"ok": True}

        func = scheduled("sweep", job)
        assert inspect.iscoroutinefunction(func)

    def test_a_coroutine_job_is_recorded(self, scheduled, db):
        async def job():
            return {"ok": True}

        func = scheduled("sweep", job)
        assert asyncio.run(func()) == {"ok": True}
        row = _row(db)
        assert row["status"] == "completed"
        assert json.loads(row["context"])["result"] == {"ok": True}

    def test_a_failing_coroutine_job_is_recorded_failed(self, scheduled, db):
        async def job():
            raise ValueError("bad input")

        func = scheduled("sweep", job)
        with pytest.raises(ValueError):
            asyncio.run(func())
        assert _row(db)["status"] == "failed"


class TestRecordingNeverBreaksTheJob:
    def test_a_database_outage_does_not_stop_the_job(self, scheduled, monkeypatch):
        from robothor.db import connection

        def _down(*a, **kw):
            raise ConnectionError("database unavailable")

        monkeypatch.setattr(connection, "get_connection", _down)
        calls: list[int] = []

        def job():
            calls.append(1)
            return {"ok": True}

        func = scheduled("sweep", job)
        assert func() == {"ok": True}
        assert calls == [1]
