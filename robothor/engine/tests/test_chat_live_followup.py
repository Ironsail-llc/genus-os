"""The Helm web chat: a message sent while the conversation's run works joins it.

Joining is opt-in (``join_running``) because the unflagged contract is load-
bearing: two plain ``/chat/send`` calls on one session both run and both end in
``done`` (test_concurrent_session.py), and API clients depend on that.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine.chat import _sessions, get_shared_session, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType

KEY = "live:main:e2e"


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


def _run(text: str) -> AgentRun:
    return AgentRun(status=RunStatus.COMPLETED, output_text=text, trigger_type=TriggerType.WEBCHAT)


RID1 = "11111111-1111-4111-8111-111111111111"
RID2 = "22222222-2222-4222-8222-222222222222"


async def _send(client, message: str, **extra) -> list[tuple[str, dict]]:
    res = await client.post("/chat/send", json={"session_key": KEY, "message": message, **extra})
    assert res.status_code == 200, res.text
    return _events(res.text)


async def _add(client, message: str, running: str = RID1) -> list[tuple[str, dict]]:
    """What the Helm sends while a turn runs: the text and the turn it is for."""
    return await _send(client, message, join_running=True, running_request_id=running)


@pytest.mark.asyncio
async def test_a_flagged_message_joins_the_running_turn(client, mock_runner):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await release.wait()
        kwargs["live_inbox"].take()
        return _run("draft, including Y")

    mock_runner.execute = AsyncMock(side_effect=execute)
    first = asyncio.create_task(_send(client, "draft it", request_id=RID1))
    await started.wait()

    joined = await _add(client, "also Y")
    release.set()
    events = await first

    assert [e for e, _ in joined] == ["followup_joined"]
    assert mock_runner.execute.await_count == 1
    assert [e for e, _ in events if e == "done"] == ["done"]
    history = get_shared_session(KEY).history
    users = [m["content"] for m in history if m["role"] == "user"]
    assert len(users) == 1 and "draft it" in users[0] and "also Y" in users[0]


@pytest.mark.asyncio
async def test_what_the_turn_never_took_comes_back_to_the_browser(client, mock_runner):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await release.wait()  # returns without another safe point
        return _run("draft")

    mock_runner.execute = AsyncMock(side_effect=execute)
    first = asyncio.create_task(_send(client, "draft it", request_id=RID1))
    await started.wait()
    await _add(client, "also Y")
    release.set()
    events = await first

    pending = [d for e, d in events if e == "pending_followups"]
    assert pending == [{"messages": ["also Y"]}]
    users = [m["content"] for m in get_shared_session(KEY).history if m["role"] == "user"]
    assert users == ["draft it"], "not claimed as heard by a turn that never took it"


@pytest.mark.asyncio
async def test_an_interim_answer_is_its_own_event_and_streaming_restarts(client, mock_runner):
    async def execute(**kwargs):
        on_content, inbox = kwargs["on_content"], kwargs["live_inbox"]
        await on_content("Here is the draft.")
        await inbox.on_interim("Here is the draft.")
        await on_content("Revised")  # the next call's cumulative text starts again
        return _run("Revised")

    mock_runner.execute = AsyncMock(side_effect=execute)
    events = await _send(client, "draft it")

    names = [e for e, _ in events]
    assert names.index("interim") < names.index("done")
    deltas_after = [d["text"] for e, d in events[names.index("interim") :] if e == "delta"]
    assert "".join(deltas_after) == "Revised"


@pytest.mark.asyncio
async def test_queued_when_the_running_turn_cannot_take_it(client, mock_runner):
    """A plan run, or another user's turn: the browser holds the message."""
    session = get_shared_session(KEY)
    session.active_task = asyncio.create_task(asyncio.sleep(5))  # no inbox parked
    try:
        joined = await _add(client, "also Y")
    finally:
        session.active_task.cancel()
    assert [e for e, _ in joined] == ["followup_queued"]
    mock_runner.execute.assert_not_called()


@pytest.mark.asyncio
async def test_a_flagged_message_never_starts_a_run_itself(client, mock_runner):
    """The turn ended while it was on its way: queued, and the browser sends it
    as an ordinary turn, which has a stream to render the reply into."""
    mock_runner.execute = AsyncMock(return_value=_run("hi"))
    events = await _add(client, "hello")
    assert [e for e, _ in events] == ["followup_queued"]
    mock_runner.execute.assert_not_called()


@pytest.mark.asyncio
async def test_stop_discards_what_was_added(client, mock_runner):
    started = asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await asyncio.sleep(30)

    mock_runner.execute = AsyncMock(side_effect=execute)
    first = asyncio.create_task(_send(client, "long job", request_id=RID1))
    await started.wait()
    await _add(client, "and this")
    # The durable stop row is the runtime's concern, not this test's.
    with patch("robothor.engine.runtime.controls.issue_request"):
        res = await client.post("/chat/abort", json={"session_key": KEY, "request_id": RID1})
    assert res.status_code == 200
    events = await first

    assert not [d for e, d in events if e == "pending_followups"]


@pytest.mark.asyncio
async def test_a_stop_that_stops_nothing_keeps_what_was_added(client, mock_runner):
    """Review #1: an abort naming another request cancels nothing, and must not
    discard the running turn's follow-ups either."""
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await release.wait()
        return _run("draft")

    mock_runner.execute = AsyncMock(side_effect=execute)
    first = asyncio.create_task(_send(client, "draft it", request_id=RID1))
    await started.wait()
    await _add(client, "also Y")
    with patch("robothor.engine.runtime.controls.issue_request"):
        res = await client.post("/chat/abort", json={"session_key": KEY, "request_id": RID2})
    assert res.json()["aborted"] is False
    release.set()
    events = await first

    assert [d for e, d in events if e == "pending_followups"] == [{"messages": ["also Y"]}]


@pytest.mark.asyncio
async def test_a_follow_up_joins_the_turn_it_names_not_the_newest(client, mock_runner):
    """Review #2: two tabs, two turns. Tab 1's addition belongs to tab 1's turn."""
    started = {RID1: asyncio.Event(), RID2: asyncio.Event()}
    release = asyncio.Event()
    took: dict[str, list[str]] = {}

    async def execute(**kwargs):
        rid = RID1 if kwargs["message"] == "tab one" else RID2
        started[rid].set()
        await release.wait()
        took[rid] = [i.text for i in kwargs["live_inbox"].take()]
        return _run("ok")

    mock_runner.execute = AsyncMock(side_effect=execute)
    one = asyncio.create_task(_send(client, "tab one", request_id=RID1))
    await started[RID1].wait()
    two = asyncio.create_task(_send(client, "tab two", request_id=RID2))
    await started[RID2].wait()

    assert [e for e, _ in await _add(client, "for tab one", running=RID1)] == ["followup_joined"]
    release.set()
    await asyncio.gather(one, two)
    assert took == {RID1: ["for tab one"], RID2: []}


@pytest.mark.asyncio
async def test_a_follow_up_naming_no_turn_is_queued(client, mock_runner):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await release.wait()
        return _run("ok")

    mock_runner.execute = AsyncMock(side_effect=execute)
    first = asyncio.create_task(_send(client, "draft it", request_id=RID1))
    await started.wait()
    events = await _send(client, "also Y", join_running=True)
    release.set()
    await first
    assert [e for e, _ in events] == ["followup_queued"]
