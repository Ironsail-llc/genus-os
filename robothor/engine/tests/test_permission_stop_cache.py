"""Permission rules and the durable-stop verdict are read once, not once per call.

Before this, every tool call ran the role_permissions query (plus the
user_permissions one for a known user), and the durable-stop recursive CTE ran
before every provider attempt AND before every tool call. These tests count the
statements an iteration costs and pin the staleness bounds the caches promise:

* permissions: invalidated at once by an in-process write; otherwise re-read
  after ``PERMISSION_CACHE_TTL_SECONDS``; a failed read is never cached.
* durable stop: the provider attempt (the start of a loop iteration) always
  reads; the tool calls of that iteration reuse its verdict. A stop issued in
  this process is honoured at the very next tool call; one committed elsewhere
  at the next provider attempt or after ``STOP_REUSE_SECONDS``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine import permissions
from robothor.engine.runtime import controls

# The engine conftest swaps in a seeded evaluator; these tests need the real one.
_REAL_CHECK = permissions.check_tool_permission
_REAL_ISSUE = controls.issue
_REAL_ISSUE_REQUEST = controls.issue_request

TENANT = "test-tenant"
RUN_ID = "00000000-0000-4000-8000-000000000001"


# ── A counting stand-in for robothor.db.connection.get_connection ──────────


class _CountingDB:
    """Answers the permission queries from in-memory rows and counts them."""

    def __init__(self) -> None:
        self.role_rows: list[tuple[str, str, str]] = [("*", "allow", "__default__")]
        self.user_rows: list[tuple[str, str]] = []
        self.statements: list[str] = []
        self.fail = False

    def count(self, table: str) -> int:
        return sum(1 for sql in self.statements if f"FROM {table}" in sql)

    @contextmanager
    def connect(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail:
            raise OSError("synthetic database outage")
        db = self

        class _Cursor:
            last = ""

            def execute(self, sql: str, params: Any = None) -> None:
                db.statements.append(sql)
                self.last = sql

            def fetchall(self) -> list[Any]:
                if "FROM user_permissions" in self.last:
                    return list(db.user_rows)
                return list(db.role_rows)

        class _Conn:
            def cursor(self) -> _Cursor:
                return _Cursor()

        yield _Conn()


def _reset(module: Any, hook: str) -> None:
    getattr(module, hook, lambda: None)()


@pytest.fixture
def db(monkeypatch) -> Any:
    fake = _CountingDB()
    monkeypatch.setattr(permissions, "check_tool_permission", _REAL_CHECK)
    _reset(permissions, "invalidate_permission_cache")
    with patch("robothor.db.connection.get_connection", fake.connect):
        yield fake
    _reset(permissions, "invalidate_permission_cache")


@pytest.fixture
def stop_reads(monkeypatch) -> Any:
    """Replace the durable-stop query with a counter whose answer a test can flip."""
    state = SimpleNamespace(calls=0, stopped=False)

    def stopped(tenant: str, run_id: str) -> bool:
        state.calls += 1
        return state.stopped

    monkeypatch.setattr(controls, "stopped", stopped)
    _reset(controls, "invalidate_stop_cache")
    yield state
    _reset(controls, "invalidate_stop_cache")


@contextmanager
def _iteration_activity() -> Any:
    """The runtime activity a real run has: one registered session for RUN_ID."""
    from robothor.engine.runtime.activity import Activity, current

    session = SimpleNamespace(run=SimpleNamespace(tenant_id=TENANT), run_id=RUN_ID)
    loop = asyncio.get_running_loop()
    activity = Activity(task=asyncio.current_task(), loop=loop, sessions={RUN_ID: session})  # type: ignore[arg-type]
    token = current.set(activity)
    try:
        yield
    finally:
        current.reset(token)


def _ctx() -> Any:
    from robothor.engine.tools.dispatch import ToolContext

    return ToolContext(agent_id="example-agent", run_id=RUN_ID, tenant_id=TENANT)


async def _tool_gates(name: str, *, user_id: str = "") -> dict[str, Any] | None:
    """Both dispatch gates a tool call passes before its handler."""
    from robothor.engine.tools.dispatch import _refuse_before_handler, _runtime_denial

    refusal = await _refuse_before_handler(
        name, agent_id="example-agent", tenant_id=TENANT, user_id=user_id, user_role="user"
    )
    if refusal is not None:
        return refusal
    return await _runtime_denial(name, {}, _ctx())


# ── Query counts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_iteration_of_ten_tool_calls_costs_one_permission_and_one_stop_read(
    db, stop_reads
):
    from robothor.engine.runtime.provider_budget import assert_provider_authorized

    with patch("robothor.engine.tools.dispatch._audit_tool_call"), _iteration_activity():
        await assert_provider_authorized()
        for index in range(10):
            assert await _tool_gates(f"search_thing_{index}") is None

    assert db.count("role_permissions") == 1
    assert stop_reads.calls == 1


@pytest.mark.asyncio
async def test_a_known_user_costs_one_user_read_and_one_role_read_for_ten_calls(db, stop_reads):
    with patch("robothor.engine.tools.dispatch._audit_tool_call"):
        for index in range(10):
            assert await _tool_gates(f"search_thing_{index}", user_id="alice") is None

    assert db.count("user_permissions") == 1
    assert db.count("role_permissions") == 1


def test_rules_are_cached_per_tenant_and_role(db):
    for _ in range(3):
        assert permissions.check_tool_permission("user", TENANT, "search_x") is None
        assert permissions.check_tool_permission("user", "other-tenant", "search_x") is None
        assert permissions.check_tool_permission("admin", TENANT, "search_x") is None

    assert db.count("role_permissions") == 3


# ── Permission invalidation and staleness bound ─────────────────────────────


def test_an_in_process_invalidation_takes_effect_on_the_next_check(db):
    assert permissions.check_tool_permission("user", TENANT, "create_note") is None
    db.role_rows = [("*", "deny", "__default__")]

    permissions.invalidate_permission_cache()

    denied = permissions.check_tool_permission("user", TENANT, "create_note")
    assert denied is not None and "denied" in denied


def test_seeding_default_permissions_invalidates_the_cache(db):
    assert permissions.check_tool_permission("user", TENANT, "create_note") is None
    db.role_rows = [("*", "deny", "__default__")]

    permissions.seed_default_permissions()

    denied = permissions.check_tool_permission("user", TENANT, "create_note")
    assert denied is not None and "denied" in denied


def test_a_user_override_change_is_seen_after_invalidation(db):
    assert permissions.check_tool_permission("user", TENANT, "create_note", user_id="bob") is None
    db.user_rows = [("create_note", "deny")]

    permissions.invalidate_permission_cache()

    denied = permissions.check_tool_permission("user", TENANT, "create_note", user_id="bob")
    assert denied is not None and "bob" in denied


def test_an_out_of_process_change_is_seen_within_the_ttl(db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(permissions, "_now", lambda: clock[0])
    assert permissions.check_tool_permission("user", TENANT, "create_note") is None
    db.role_rows = [("*", "deny", "__default__")]

    clock[0] += permissions.PERMISSION_CACHE_TTL_SECONDS - 0.1
    assert permissions.check_tool_permission("user", TENANT, "create_note") is None

    clock[0] += 0.2
    denied = permissions.check_tool_permission("user", TENANT, "create_note")
    assert denied is not None and "denied" in denied
    assert permissions.PERMISSION_CACHE_TTL_SECONDS <= 5.0


def test_a_failed_read_denies_and_is_not_cached(db):
    db.fail = True
    denied = permissions.check_tool_permission("user", TENANT, "create_note")
    assert denied is not None and "unavailable" in denied

    db.fail = False
    assert permissions.check_tool_permission("user", TENANT, "create_note") is None
    assert db.count("role_permissions") == 1


# ── Durable stop: bounded staleness ─────────────────────────────────────────


@contextmanager
def _committing_cursor(*args: Any, **kwargs: Any) -> Any:
    class _Cursor:
        def execute(self, sql: str, params: Any = None) -> None:
            pass

        def fetchone(self) -> dict[str, Any]:
            return {"id": RUN_ID, "version": 1, "action": "cancel"}

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def cursor(self, **kwargs: Any) -> _Cursor:
            return _Cursor()

    yield _Conn()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["run", "request"])
async def test_an_in_process_stop_mid_iteration_is_honoured_at_the_next_tool_call(
    db, stop_reads, monkeypatch, how
):
    from robothor.engine.runtime.provider_budget import assert_provider_authorized

    monkeypatch.setattr(controls, "issue", _REAL_ISSUE)
    monkeypatch.setattr(controls, "issue_request", _REAL_ISSUE_REQUEST)
    monkeypatch.setattr(controls, "get_connection", _committing_cursor)
    monkeypatch.setattr(controls, "signal_stopped", lambda tenant, note: None)

    with patch("robothor.engine.tools.dispatch._audit_tool_call"), _iteration_activity():
        await assert_provider_authorized()
        assert await _tool_gates("search_a") is None
        assert await _tool_gates("search_b") is None

        stop_reads.stopped = True  # what the database now says
        if how == "run":
            controls.issue(TENANT, RUN_ID, "cancel", "Operator stopped execution")
        else:
            controls.issue_request(TENANT, "request-1", "Operator stopped chat")

        denial = await _tool_gates("search_c")

    assert denial is not None
    assert "durable stop denies further tool dispatch" in denial["error"]


@pytest.mark.asyncio
async def test_a_stop_committed_elsewhere_is_seen_within_the_reuse_window(
    db, stop_reads, monkeypatch
):
    from robothor.engine.runtime.provider_budget import assert_provider_authorized

    clock = [5000.0]
    monkeypatch.setattr(controls, "_now", lambda: clock[0])

    with patch("robothor.engine.tools.dispatch._audit_tool_call"), _iteration_activity():
        await assert_provider_authorized()
        stop_reads.stopped = True  # committed by another process: no local signal

        clock[0] += controls.STOP_REUSE_SECONDS - 0.1
        assert await _tool_gates("search_a") is None  # within the bound: reused

        clock[0] += 0.2
        denial = await _tool_gates("search_b")

    assert denial is not None and "durable stop" in denial["error"]
    assert controls.STOP_REUSE_SECONDS <= 10.0


@pytest.mark.asyncio
async def test_the_next_provider_attempt_always_reads_the_stop_afresh(stop_reads):
    from robothor.engine.runtime.provider_budget import (
        DurableStopError,
        assert_provider_authorized,
    )

    with _iteration_activity():
        await assert_provider_authorized()
        stop_reads.stopped = True  # committed elsewhere, inside the reuse window
        with pytest.raises(DurableStopError):
            await assert_provider_authorized()

    assert stop_reads.calls == 2


@pytest.mark.asyncio
async def test_a_stopped_verdict_is_never_forgotten_within_the_iteration(db, stop_reads):
    from robothor.engine.runtime.provider_budget import (
        DurableStopError,
        assert_provider_authorized,
    )

    stop_reads.stopped = True
    with patch("robothor.engine.tools.dispatch._audit_tool_call"), _iteration_activity():
        with pytest.raises(DurableStopError):
            await assert_provider_authorized()
        for name in ("search_a", "search_b", "search_c"):
            denial = await _tool_gates(name)
            assert denial is not None and "durable stop" in denial["error"]

    assert stop_reads.calls == 1
