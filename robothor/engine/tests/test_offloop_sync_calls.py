"""No blocking call on the engine event loop, one site at a time.

Each test replaces one synchronous DB or file call reached during a turn with a
probe that takes 200 ms and, the moment it is entered, schedules a callback on
the engine's loop. If the call runs on the loop, that callback cannot run until
the 200 ms are over; if it runs in a worker thread, it runs at once. The test
asserts the callback ran within 20 ms of being scheduled — a single slow query
on the loop stalls every other session's streaming, heartbeats and tool calls
for as long as it takes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from robothor.engine.tests.test_runner import runner  # noqa: F401

SLOW_SECONDS = 0.2
MAX_LAG_SECONDS = 0.02


class SlowProbe:
    """A 200 ms blocking call that measures how long the loop was held."""

    def __init__(self, loop: asyncio.AbstractEventLoop, result: Any = None) -> None:
        self.loop = loop
        self.result = result
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.lags: list[float] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        entered = time.monotonic()
        self.loop.call_soon_threadsafe(lambda: self.lags.append(time.monotonic() - entered))
        time.sleep(SLOW_SECONDS)
        return self.result

    async def settle(self, timeout: float = 2.0) -> None:
        """Wait until every call's loop callback has run."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calls and len(self.lags) == len(self.calls):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"probe called {len(self.calls)} time(s), {len(self.lags)} lag(s)")

    def assert_loop_free(self) -> None:
        assert self.calls, "the probed call was never reached"
        worst = max(self.lags)
        assert worst <= MAX_LAG_SECONDS, (
            f"a loop callback waited {worst * 1000:.0f} ms behind a {SLOW_SECONDS * 1000:.0f} ms "
            "blocking call: the call runs on the event loop"
        )


async def warm_executor(workers: int = 8) -> None:
    """Start the loop's default thread-pool workers before anything is timed."""
    gate = threading.Barrier(workers, timeout=2)
    await asyncio.gather(*(asyncio.to_thread(gate.wait) for _ in range(workers)))


@pytest.fixture(autouse=True)
async def _warm_pool():
    await warm_executor()


# ── chat.py: identity resolution on a cache miss ──────────────────────────


async def test_chat_send_resolves_identity_off_the_loop(monkeypatch):
    from robothor.auth.deps import AuthContext
    from robothor.engine import chat
    from robothor.identity import resolvers

    probe = SlowProbe(asyncio.get_running_loop())
    resolvers.clear_cache()
    monkeypatch.setattr("robothor.identity.resolve_identity", probe)
    monkeypatch.setattr(chat, "_runner", MagicMock())
    monkeypatch.setattr(chat, "_config", MagicMock())
    auth = AuthContext(user_id="alice", tenant_id="test-tenant", role="owner", typ="user")

    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # An empty message is refused right after identity is resolved, which
        # keeps the request to exactly the call under test.
        response = await client.post("/chat/send", json={"message": ""})

    assert response.status_code == 400
    await probe.settle()
    probe.assert_loop_free()


# ── runner.execute: manifest, identity, person and tenant lookups ─────────


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize(
    "target",
    [
        "robothor.engine.runner.load_agent_config_or_reason",
        "robothor.identity.resolve_identity",
        "robothor.engine.run_person_link.resolve_run_person_id",
        "robothor.engine.permissions.resolve_accessible_tenants",
    ],
)
async def test_execute_runs_its_lookups_off_the_loop(
    request, sample_agent_config, mock_litellm_response, monkeypatch, target
):
    from robothor.engine.models import RunStatus, TriggerType

    engine = request.getfixturevalue("runner")
    loop = asyncio.get_running_loop()
    results = {
        "robothor.engine.runner.load_agent_config_or_reason": (sample_agent_config, None),
        "robothor.identity.resolve_identity": None,
        "robothor.engine.run_person_link.resolve_run_person_id": None,
        "robothor.engine.permissions.resolve_accessible_tenants": ("test-tenant",),
    }
    probe = SlowProbe(loop, result=results[target])
    if target != "robothor.engine.runner.load_agent_config_or_reason":
        monkeypatch.setattr(
            "robothor.engine.runner.load_agent_config_or_reason",
            lambda *a, **k: (sample_agent_config, None),
        )
    monkeypatch.setattr(target, probe)

    # Interactive runs resolve their manifest at admission, already off the
    # loop (runtime/profile_lookup.py); a scheduled or manual run reads it here.
    manifest = target == "robothor.engine.runner.load_agent_config_or_reason"
    with (
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", return_value=mock_litellm_response(content="Done.")),
    ):
        run = await engine.execute(
            "test-agent",
            "hello",
            trigger_type=TriggerType.MANUAL if manifest else TriggerType.WEBCHAT,
            trigger_detail="chat:alice",
            user_id="alice",
            user_role="owner",
            tenant_id="test-tenant",
        )

    assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED)
    await probe.settle()
    probe.assert_loop_free()


# ── tools/dispatch.py: the per-tool-call audit row ────────────────────────


async def test_tool_call_audit_row_is_written_off_the_loop(monkeypatch):
    from robothor.engine.tools.dispatch import _audit_tool_call

    probe = SlowProbe(asyncio.get_running_loop())
    monkeypatch.setattr("robothor.audit.logger.log_event", probe)

    started = time.monotonic()
    _audit_tool_call("web_fetch", "example-agent", "test-tenant", user_id="alice")
    returned_after = time.monotonic() - started

    assert returned_after < MAX_LAG_SECONDS, "the audit call blocked its caller"
    await probe.settle()
    probe.assert_loop_free()
    _, kwargs = probe.calls[0]
    assert kwargs["action"] == "web_fetch"
    assert kwargs["details"]["user_id"] == "alice"


def test_tool_call_audit_docstring_does_not_claim_what_it_is_not():
    from robothor.engine.tools.dispatch import _audit_tool_call

    doc = _audit_tool_call.__doc__ or ""
    assert "queued" in doc.lower() or "writer" in doc.lower(), doc


# ── guardrail event rows (tracking + tool admission) ──────────────────────


async def test_guardrail_event_row_is_written_off_the_loop(monkeypatch):
    from robothor.engine import tracking

    probe = SlowProbe(asyncio.get_running_loop())
    monkeypatch.setattr(tracking, "_insert_guardrail_event", probe)

    started = time.monotonic()
    tracking.log_guardrail_event("run-1", "example_guardrail", "observed", reason="r")
    assert time.monotonic() - started < MAX_LAG_SECONDS

    await probe.settle()
    probe.assert_loop_free()
    assert probe.calls[0][0][0][:4] == ("run-1", 0, "example_guardrail", "observed")


async def test_tool_admission_guardrail_event_is_written_off_the_loop(monkeypatch):
    from robothor.engine import tracking
    from robothor.engine.tool_admission import _log_guardrail_event

    probe = SlowProbe(asyncio.get_running_loop())
    monkeypatch.setattr(tracking, "_insert_guardrail_event", probe)

    _log_guardrail_event(run_id="run-2", guardrail_name="example_guardrail", action="blocked")

    await probe.settle()
    probe.assert_loop_free()


def test_guardrail_event_without_a_loop_is_written_before_returning(monkeypatch):
    from robothor.engine import tracking

    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tracking, "_insert_guardrail_event", rows.append)

    tracking.log_guardrail_event("run-3", "example_guardrail", "allowed")

    assert rows and rows[0][:4] == ("run-3", 0, "example_guardrail", "allowed")


def test_audit_row_without_a_loop_is_written_before_returning(monkeypatch):
    from robothor.engine.tools.dispatch import _audit_tool_call

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("robothor.audit.logger.log_event", lambda **k: calls.append(k))

    _audit_tool_call("web_fetch", "example-agent", "test-tenant")

    assert calls and calls[0]["action"] == "web_fetch"


# ── checkpoint.py: json.dumps of the whole conversation + INSERT ──────────


async def test_checkpoint_save_runs_off_the_loop():
    from robothor.engine.checkpoint import save_iteration

    probe = SlowProbe(asyncio.get_running_loop(), result=True)
    checkpoint = MagicMock()
    checkpoint.should_checkpoint.return_value = True
    checkpoint.save = probe
    session = MagicMock()
    session.todo_list = None
    session._step_counter = 3
    session.messages = [{"role": "user", "content": "hello"}]

    await save_iteration(
        checkpoint,
        session,
        agent_config=MagicMock(),
        scratchpad=None,
        plan_result=None,
        hook_registry=None,
    )

    await probe.settle()
    probe.assert_loop_free()
    _, kwargs = probe.calls[0]
    assert kwargs["step_number"] == 3
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]


# ── run_finalizer._finish_run: run-end bookkeeping ────────────────────────


@pytest.mark.parametrize("site", ["record_run", "timeline", "previous_output"])
async def test_finish_run_bookkeeping_runs_off_the_loop(request, monkeypatch, site):
    from robothor.engine.models import AgentRun, RunStatus
    from robothor.engine.run_finalizer import RunFinalizationMixin

    engine = request.getfixturevalue("runner")
    monkeypatch.setattr(engine, "_persist_run_sync", MagicMock())
    monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "observe")
    loop = asyncio.get_running_loop()

    probe = SlowProbe(loop, result="Fleet health: 48.6%" if site == "previous_output" else None)
    quiet = MagicMock(return_value=None)
    monkeypatch.setattr(
        "robothor.engine.recent_actions.record_run", probe if site == "record_run" else quiet
    )
    monkeypatch.setattr(
        "robothor.engine.run_person_link.emit_run_timeline_activity",
        probe if site == "timeline" else quiet,
    )
    monkeypatch.setattr(
        RunFinalizationMixin,
        "_previous_delivered_output",
        staticmethod(probe if site == "previous_output" else quiet),
    )
    monkeypatch.setattr("robothor.engine.tracking._insert_guardrail_event", MagicMock())

    def finished_run() -> AgentRun:
        run = AgentRun(agent_id="example-agent")
        run.status = RunStatus.COMPLETED
        run.output_text = "Fleet health: 52.8% (↓0.5pp WoW)"
        run.person_id = "person-1"
        return run

    # Warm up first, so what is measured is the call under test and not this
    # test loop's fresh thread pool starting its workers (Thread.start() waits
    # on the loop thread) or a module being imported for the first time.
    with monkeypatch.context() as warm:
        for name in ("record_run", "emit_run_timeline_activity"):
            module = "recent_actions" if name == "record_run" else "run_person_link"
            warm.setattr(f"robothor.engine.{module}.{name}", quiet)
        warm.setattr(RunFinalizationMixin, "_previous_delivered_output", staticmethod(quiet))
        engine._finish_run(finished_run())
        await asyncio.sleep(0.1)

    run = finished_run()
    started = time.monotonic()
    finished = engine._finish_run(run)
    returned_after = time.monotonic() - started

    assert finished is run
    assert returned_after < 0.05, f"_finish_run held its caller {returned_after * 1000:.0f} ms"
    await probe.settle()
    probe.assert_loop_free()
