"""Web chat: steer and stop the session's live run, behind ROBOTHOR_STEER_EVERYWHERE.

``/chat/abort`` cancels the turn's task outright. ``/chat/steer`` and
``/chat/interrupt`` reach the run itself: a steer joins it at the next
iteration boundary as an operator steering update, and an interrupt ends it at
that boundary as CANCELLED, the same semantics Telegram's ``/steer`` and the
run-id REST routes have always had. The session key is resolved exactly as
``/chat/send`` resolves it, so a caller can only ever reach their own run.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine import live_control, session_registry
from robothor.engine.chat import _sessions, init_chat, router
from robothor.engine.loop_guards import GuardState, check_iteration_guards
from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.request_runtime import observe_request
from robothor.engine.session import AgentSession

KEY = "steer:main:e2e"


@pytest.fixture(autouse=True)
def _clear_registry():
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)
    yield
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(live_control, "steer_everywhere_enabled", lambda: True)


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
async def client(engine_config, mock_runner):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    with (
        patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()),
        patch("robothor.memory.conversation_ingest.ingest_conversation_session", new=AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    _sessions.clear()


def _events(body: str) -> list[tuple[str, dict]]:
    out, event = [], ""
    for line in body.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            out.append((event, json.loads(line[6:])))
    return out


class _FakeRun:
    """What the runner does around one iteration boundary, with a real session."""

    def __init__(self) -> None:
        self.live = asyncio.Event()
        self.release = asyncio.Event()
        self.session: AgentSession | None = None
        self.stopped: bool | None = None

    async def execute(self, **kwargs):
        session = AgentSession("main", TriggerType.WEBCHAT, None, kwargs["tenant_id"])
        session.start("system", kwargs["message"], [])
        self.session = session
        async with observe_request(session, None):
            self.live.set()
            await self.release.wait()
            self.stopped = check_iteration_guards(
                session,
                SimpleNamespace(id="main"),
                watchdog=None,
                wallclock_deadline=None,
                wallclock_ceiling=0,
                state=GuardState(),
            )
        if session.was_interrupted:
            return session.cancelled(session._interrupt_note)
        return session.complete("done")


def _steering_turns(session: AgentSession) -> list[str]:
    return [
        m["content"]
        for m in session.messages
        if m["role"] == "user" and str(m["content"]).startswith("[operator steering update]")
    ]


async def _start(client, mock_runner) -> tuple[_FakeRun, asyncio.Task]:
    fake = _FakeRun()
    mock_runner.execute = AsyncMock(side_effect=fake.execute)
    task = asyncio.create_task(
        client.post("/chat/send", json={"session_key": KEY, "message": "draft the report"})
    )
    await asyncio.wait_for(fake.live.wait(), timeout=5)
    return fake, task


@pytest.mark.asyncio
async def test_a_steer_mid_run_lands_once_at_the_next_iteration_boundary(
    client, mock_runner, flag_on
):
    fake, task = await _start(client, mock_runner)

    res = await client.post("/chat/steer", json={"session_key": KEY, "text": "use the EU region"})
    assert res.status_code == 200
    assert res.json()["ok"] is True and res.json()["found"] is True
    assert _steering_turns(fake.session) == [], "nothing lands before the boundary"

    fake.release.set()
    sent = await task
    assert fake.stopped is False
    assert _steering_turns(fake.session) == ["[operator steering update]\nuse the EU region"]
    done = [d for e, d in _events(sent.text) if e == "done"]
    assert done and done[0]["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_an_interrupt_ends_the_run_cancelled_at_the_boundary(client, mock_runner, flag_on):
    fake, task = await _start(client, mock_runner)

    res = await client.post("/chat/interrupt", json={"session_key": KEY})
    assert res.status_code == 200
    assert res.json()["ok"] is True and res.json()["found"] is True

    fake.release.set()
    sent = await task
    assert fake.stopped is True
    done = [d for e, d in _events(sent.text) if e == "done"]
    assert done and done[0]["status"] == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_with_nothing_running_the_steer_finds_nothing(client, flag_on):
    res = await client.post("/chat/steer", json={"session_key": KEY, "text": "hello"})
    assert res.status_code == 200
    assert res.json() == {"ok": False, "found": False}


@pytest.mark.asyncio
async def test_a_steer_needs_text(client, flag_on):
    res = await client.post("/chat/steer", json={"session_key": KEY, "text": "  "})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_another_tenants_run_on_the_same_key_is_out_of_reach(client, flag_on):
    session = AgentSession("main", TriggerType.WEBCHAT, None, "another-tenant")
    session.start("system", "x", [])
    with live_control.conversation_scope(KEY):
        async with observe_request(session, None):
            steer = await client.post("/chat/steer", json={"session_key": KEY, "text": "x"})
            stop = await client.post("/chat/interrupt", json={"session_key": KEY})
    assert steer.json()["found"] is False and stop.json()["found"] is False
    assert _steering_turns(session) == []
    assert session.consume_interrupt() is None


@pytest.mark.asyncio
async def test_the_flag_is_reported(client, flag_on):
    res = await client.get("/chat/steer")
    assert res.status_code == 200 and res.json() == {"enabled": True}


@pytest.mark.asyncio
async def test_flag_off_the_routes_are_not_there(client, monkeypatch):
    monkeypatch.setattr(live_control, "steer_everywhere_enabled", lambda: False)
    assert (await client.post("/chat/steer", json={"text": "x"})).status_code == 404
    assert (await client.post("/chat/interrupt", json={})).status_code == 404
    res = await client.get("/chat/steer")
    assert res.status_code == 200 and res.json() == {"enabled": False}
