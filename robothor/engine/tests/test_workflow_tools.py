"""Agent tools that list, start and report on workflows.

A fake engine stands in for ``WorkflowEngine`` (installed through the module
registry the daemon fills), and the tool DB connection is faked so the tests
can see which tenant every query was scoped to.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.models import (
    RunStatus,
    WorkflowDef,
    WorkflowRun,
    WorkflowTriggerDef,
)
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.workflow_registry import get_workflow_engine, set_workflow_engine

TENANT = "test-tenant"


class FakeEngine:
    """Just enough of WorkflowEngine for the tools: definitions + execute."""

    def __init__(
        self,
        status: RunStatus = RunStatus.COMPLETED,
        error_message: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self._workflows = {
            "daily-report": WorkflowDef(
                id="daily-report",
                name="Daily report",
                description="Summarise the day",
                triggers=[WorkflowTriggerDef(type="cron", cron="0 7 * * *")],
            ),
            "on-signup": WorkflowDef(
                id="on-signup",
                name="On signup",
                triggers=[WorkflowTriggerDef(type="hook", stream="crm", event_type="signup")],
            ),
        }
        self.status = status
        self.error_message = error_message
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.finished = asyncio.Event()
        self.config = SimpleNamespace(tenant_id=TENANT)
        self.cancel = False

    def list_workflows(self) -> list[WorkflowDef]:
        return list(self._workflows.values())

    def get_workflow(self, workflow_id: str) -> WorkflowDef | None:
        return self._workflows.get(workflow_id)

    async def execute(self, workflow_id: str, **kwargs: Any) -> WorkflowRun:
        self.calls.append({"workflow_id": workflow_id, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.cancel:
            raise asyncio.CancelledError
        run = WorkflowRun(
            workflow_id=workflow_id,
            status=self.status,
            error_message=self.error_message,
        )
        if self.status != RunStatus.SKIPPED and kwargs.get("run_id"):
            run.id = kwargs["run_id"]
        self.finished.set()
        return run


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.db.queries.append((sql, tuple(params)))
        self._rows = self.db.results.pop(0) if self.db.results else []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeDB:
    """Records every query; answers each execute with the next queued row set."""

    def __init__(self, results: list[list[tuple[Any, ...]]] | None = None) -> None:
        self.results = list(results or [])
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> FakeDB:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        return None


@pytest.fixture
def engine():
    fake = FakeEngine()
    previous = get_workflow_engine()
    set_workflow_engine(fake)
    yield fake
    set_workflow_engine(previous)


def _ctx(**kwargs: Any) -> ToolContext:
    base: dict[str, Any] = {"agent_id": "main", "run_id": "agent-run-1", "tenant_id": TENANT}
    base.update(kwargs)
    return ToolContext(**base)


def _handlers() -> dict[str, Any]:
    from robothor.engine.tools.handlers.workflows import HANDLERS

    return HANDLERS


def _patch_db(db: FakeDB):
    return patch("robothor.engine.tools.dispatch.get_db", return_value=db)


# ── registration ─────────────────────────────────────────────────────────


def test_tools_are_registered_with_schemas_and_read_only_split() -> None:
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    handlers = builtin_handlers()
    schemas = get_engine_schemas()
    for name in ("list_workflows", "run_workflow", "workflow_runs"):
        assert name in handlers
        assert name in schemas
    assert "list_workflows" in READONLY_TOOLS
    assert "workflow_runs" in READONLY_TOOLS
    assert "run_workflow" not in READONLY_TOOLS


def test_registry_round_trip() -> None:
    sentinel = object()
    previous = get_workflow_engine()
    try:
        set_workflow_engine(sentinel)
        assert get_workflow_engine() is sentinel
    finally:
        set_workflow_engine(previous)


# ── run_workflow ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_unknown_workflow(engine: FakeEngine) -> None:
    out = await _handlers()["run_workflow"]({"workflow_id": "nope"}, _ctx())
    assert out["error"] == "unknown_workflow"
    assert engine.calls == []


@pytest.mark.asyncio
async def test_run_without_engine_is_an_error() -> None:
    previous = get_workflow_engine()
    set_workflow_engine(None)
    try:
        out = await _handlers()["run_workflow"]({"workflow_id": "daily-report"}, _ctx())
    finally:
        set_workflow_engine(previous)
    assert "error" in out


@pytest.mark.asyncio
async def test_run_already_running(engine: FakeEngine) -> None:
    engine.status = RunStatus.SKIPPED
    engine.error_message = "Skipped: workflow already running"
    out = await _handlers()["run_workflow"]({"workflow_id": "daily-report"}, _ctx())
    assert out == {"status": "already_running"}


@pytest.mark.asyncio
async def test_run_wait_returns_final_status(engine: FakeEngine) -> None:
    engine.status = RunStatus.FAILED
    engine.error_message = "Step 's1' failed: boom"
    engine.delay = 0.05
    out = await _handlers()["run_workflow"](
        {"workflow_id": "daily-report", "wait_seconds": 5}, _ctx()
    )
    assert out["status"] == "failed"
    assert out["error_message"] == "Step 's1' failed: boom"
    assert out["run_id"] == engine.calls[0]["run_id"]


@pytest.mark.asyncio
async def test_run_without_wait_returns_started_and_keeps_running(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.engine.tools.handlers import workflows

    monkeypatch.setattr(workflows, "START_GRACE_SECONDS", 0.01)
    engine.delay = 0.2
    out = await _handlers()["run_workflow"]({"workflow_id": "daily-report"}, _ctx())
    assert out["status"] == "started"
    assert out["run_id"] == engine.calls[0]["run_id"]
    # The run was not cancelled when the tool stopped waiting.
    await asyncio.wait_for(engine.finished.wait(), timeout=2)


@pytest.mark.asyncio
async def test_run_caps_wait_at_120_seconds(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    real_wait = asyncio.wait

    async def spy_wait(tasks: Any, timeout: float | None = None, **kw: Any) -> Any:
        seen.append(timeout or 0)
        return await real_wait(tasks, timeout=timeout, **kw)

    monkeypatch.setattr(asyncio, "wait", spy_wait)
    await _handlers()["run_workflow"]({"workflow_id": "daily-report", "wait_seconds": 9999}, _ctx())
    assert seen and max(seen) == 120


@pytest.mark.asyncio
async def test_run_passes_identity_trigger_and_payload(engine: FakeEngine) -> None:
    await _handlers()["run_workflow"](
        {"workflow_id": "daily-report", "payload": {"x": 1}, "wait_seconds": 1}, _ctx()
    )
    call = engine.calls[0]
    assert call["trigger_type"] == "agent"
    assert call["trigger_detail"] == "agent:main:agent-run-1"
    assert call["initial_context"] == {"event": {"payload": {"x": 1}}}
    # No user on the context: the agent's own service identity.
    assert call["user_id"] == "service:agent:main"
    assert call["user_role"] == "service"


@pytest.mark.asyncio
async def test_run_propagates_the_users_identity(engine: FakeEngine) -> None:
    await _handlers()["run_workflow"](
        {"workflow_id": "daily-report", "wait_seconds": 1},
        _ctx(user_id="alice", user_role="member"),
    )
    call = engine.calls[0]
    assert call["user_id"] == "alice"
    assert call["user_role"] == "member"


@pytest.mark.asyncio
async def test_run_refuses_a_caller_in_another_tenant(engine: FakeEngine) -> None:
    out = await _handlers()["run_workflow"](
        {"workflow_id": "daily-report"}, _ctx(tenant_id="child-tenant")
    )
    assert out == {"error": "workflow_tenant_mismatch", "run_tenant": TENANT}
    assert engine.calls == []


@pytest.mark.asyncio
async def test_run_reports_a_cancelled_run(engine: FakeEngine) -> None:
    engine.cancel = True
    out = await _handlers()["run_workflow"](
        {"workflow_id": "daily-report", "wait_seconds": 1}, _ctx()
    )
    assert out == {"status": "cancelled", "run_id": engine.calls[0]["run_id"]}


@pytest.mark.asyncio
async def test_run_keeps_a_user_without_a_role_as_given(engine: FakeEngine) -> None:
    await _handlers()["run_workflow"](
        {"workflow_id": "daily-report", "wait_seconds": 1}, _ctx(user_id="alice")
    )
    call = engine.calls[0]
    assert call["user_id"] == "alice"
    assert call["user_role"] == ""


# ── list_workflows ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_workflows_merges_run_facts_for_the_tenant(engine: FakeEngine) -> None:
    started = datetime(2026, 1, 2, 7, 0, tzinfo=UTC)
    db = FakeDB([[("daily-report", "failed", started, "cron", 3)]])
    with _patch_db(db):
        out = await _handlers()["list_workflows"]({}, _ctx())

    by_id = {w["id"]: w for w in out["workflows"]}
    daily = by_id["daily-report"]
    assert daily["name"] == "Daily report"
    assert daily["description"] == "Summarise the day"
    assert daily["triggers"] == [{"type": "cron", "cron": "0 7 * * *"}]
    assert daily["last_run"] == {
        "status": "failed",
        "started_at": started.isoformat(),
        "trigger_type": "cron",
    }
    assert daily["failures_7d"] == 3

    signup = by_id["on-signup"]
    assert signup["triggers"] == [{"type": "hook", "stream": "crm", "event_type": "signup"}]
    assert signup["last_run"] is None
    assert signup["failures_7d"] == 0

    assert db.queries, "run facts must come from the database"
    for sql, params in db.queries:
        assert "tenant_id" in sql
        assert TENANT in params


# ── workflow_runs ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_runs_are_tenant_scoped_with_steps(engine: FakeEngine) -> None:
    started = datetime(2026, 1, 2, 7, 0, tzinfo=UTC)
    done = datetime(2026, 1, 2, 7, 5, tzinfo=UTC)
    db = FakeDB(
        [
            [("run-1", "completed", "agent", "agent:main:r", started, done, None)],
            [("run-1", "fetch", "completed"), ("run-1", "send", "completed")],
        ]
    )
    with _patch_db(db):
        out = await _handlers()["workflow_runs"](
            {"workflow_id": "daily-report", "limit": 5}, _ctx()
        )

    assert out["runs"] == [
        {
            "run_id": "run-1",
            "status": "completed",
            "trigger_type": "agent",
            "trigger_detail": "agent:main:r",
            "started_at": started.isoformat(),
            "completed_at": done.isoformat(),
            "error_message": None,
            "steps": [
                {"id": "fetch", "status": "completed"},
                {"id": "send", "status": "completed"},
            ],
        }
    ]
    runs_sql, runs_params = db.queries[0]
    assert "tenant_id" in runs_sql
    assert TENANT in runs_params
    assert "daily-report" in runs_params
    assert 5 in runs_params


@pytest.mark.asyncio
async def test_workflow_runs_requires_an_id(engine: FakeEngine) -> None:
    out = await _handlers()["workflow_runs"]({}, _ctx())
    assert "error" in out
