"""The answer is released before run finalization (``ROBOTHOR_EARLY_DONE``).

A chat turn's final text is settled long before the run is: after it come
telemetry, post-run guardrails, verification, the recent-actions file, the
timeline insert, the run-row persist, the history save, the embedding and the
memory ingest. None of that changes what the person is shown, yet the SSE
``done`` event and the Telegram reply waited for all of it.

With the flag on, ``AgentRunner.execute(on_complete=...)`` awaits the callback
the moment the completed run's text is final, and runs the rest of
finalization as a task under the engine task registry, so ``genus run``'s drain
and daemon shutdown still wait for it.

One part of finalization CAN change the shown result: an output-altering
post-run policy (the deliverable contract in enforce mode turns a completed run
into a failed one). That part still runs before the callback.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.chat_result import result_text
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry, reset_task_registry

SLOW_FINISH_S = 0.4


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_task_registry()
    yield
    reset_task_registry()


@pytest.fixture
def early_done_on(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EARLY_DONE", "1")


@pytest.fixture
def early_done_off(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_EARLY_DONE", raising=False)


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = []
        registry.get_tool_names.return_value = []
        registry.execute = AsyncMock(return_value={"ok": True})
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="example-agent",
        name="Example Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=0,
        stall_timeout_seconds=0,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
    )


def _response(content: str = "The answer is 42.") -> MagicMock:
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


class _Timeline:
    """Records named moments, so ordering is asserted on the clock."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.perf_counter() - self.t0)


def _slow_finish(timeline: _Timeline, *, raises: Exception | None = None):
    """A stand-in for the blocking ``_finish_run``: sync, slow, then done."""

    def fake(self, run, trace=None, agent_config=None, session=None, spawn_context=None):
        timeline.mark("finish_start")
        time.sleep(SLOW_FINISH_S)
        if raises is not None:
            timeline.mark("finish_raised")
            raise raises
        timeline.mark("finish_end")
        return run

    return fake


async def _execute(runner, agent_config, **kwargs) -> AgentRun:
    with (
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", new=AsyncMock(return_value=_response())),
    ):
        return await runner.execute(
            "example-agent", "what is the answer?", agent_config=agent_config, **kwargs
        )


# ── The runner contract ──────────────────────────────────────────────────


@pytest.mark.usefixtures("_mock_run_persistence")
class TestRunnerReleasesTheAnswerFirst:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_on_complete_fires_before_a_slow_finish_run(self, runner, agent_config):
        timeline = _Timeline()
        seen: list[AgentRun] = []

        async def on_complete(run: AgentRun) -> None:
            timeline.mark("on_complete")
            seen.append(run)

        with patch.object(AgentRunner, "_finish_run", _slow_finish(timeline)):
            run = await _execute(runner, agent_config, on_complete=on_complete)
            timeline.mark("execute_returned")
            assert "finish_end" not in timeline.marks, timeline.marks
            assert get_task_registry().pending_count >= 1
            await get_task_registry().drain(timeout=5.0)

        assert seen == [run]
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "The answer is 42."
        assert run.duration_ms is not None
        # The answer went out first; finalization finished afterwards.
        assert timeline.marks["on_complete"] < timeline.marks["finish_end"]
        assert timeline.marks["execute_returned"] < timeline.marks["finish_end"]
        assert timeline.marks["on_complete"] < SLOW_FINISH_S / 2

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_off")
    async def test_flag_off_keeps_todays_order(self, runner, agent_config):
        timeline = _Timeline()
        on_complete = AsyncMock()

        with patch.object(AgentRunner, "_finish_run", _slow_finish(timeline)):
            await _execute(runner, agent_config, on_complete=on_complete)
            timeline.mark("execute_returned")

        assert timeline.marks["finish_end"] < timeline.marks["execute_returned"]
        on_complete.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_no_callback_keeps_todays_order(self, runner, agent_config):
        """Callers that did not opt in (scheduler, workflows) get a finalized run."""
        timeline = _Timeline()

        with patch.object(AgentRunner, "_finish_run", _slow_finish(timeline)):
            await _execute(runner, agent_config)
            timeline.mark("execute_returned")

        assert timeline.marks["finish_end"] < timeline.marks["execute_returned"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_drain_waits_for_background_finalization(self, runner, agent_config):
        timeline = _Timeline()

        with patch.object(AgentRunner, "_finish_run", _slow_finish(timeline)):
            await _execute(runner, agent_config, on_complete=AsyncMock())
            assert "finish_end" not in timeline.marks
            await get_task_registry().drain(timeout=5.0)

        assert "finish_end" in timeline.marks
        assert get_task_registry().pending_count == 0

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_finalization_failure_is_logged_and_recorded(self, runner, agent_config, caplog):
        timeline = _Timeline()
        on_complete = AsyncMock()
        persist = AsyncMock()

        with (
            patch.object(
                AgentRunner, "_finish_run", _slow_finish(timeline, raises=RuntimeError("disk full"))
            ),
            patch.object(AgentRunner, "_persist_run", persist),
        ):
            run = await _execute(runner, agent_config, on_complete=on_complete)
            await get_task_registry().drain(timeout=5.0)

        on_complete.assert_awaited_once()
        assert run.status == RunStatus.COMPLETED  # the answer stands
        assert "disk full" in (run.error_message or "")
        persist.assert_awaited()
        assert persist.await_args.args[-1] is run
        assert any("disk full" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("_mock_run_persistence")
class TestOutputAlteringPolicyRunsBeforeTheAnswer:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_a_blocking_post_run_policy_still_suppresses_the_text(self, runner, agent_config):
        calls: list[str] = []

        def blocking_policy(run, session, workspace) -> None:
            calls.append(run.id)
            run.status = RunStatus.FAILED
            run.error_message = "Deliverable contract not satisfied: report.md missing"

        shown: list[str] = []

        async def on_complete(run: AgentRun) -> None:
            shown.append(result_text(run))

        with patch(
            "robothor.engine.deliverable_verdict.record_deliverable_verdicts",
            side_effect=blocking_policy,
        ):
            run = await _execute(runner, agent_config, on_complete=on_complete)
            await get_task_registry().drain(timeout=5.0)

        assert len(shown) == 1
        assert shown[0].startswith("[Run failed:"), shown
        assert "The answer is 42." not in shown[0]
        assert run.status == RunStatus.FAILED
        # Applied once, before the answer; background finalization skips it.
        assert calls == [run.id]


# ── The webchat SSE stream ───────────────────────────────────────────────


class _ContractRunner:
    """A runner that keeps ``execute``'s early-done contract, with a slow finish.

    With ``on_complete``: awaits it with the completed run, schedules the slow
    finalization under the task registry and returns. Without it: finalizes
    inline, as the real runner does today.
    """

    def __init__(self, engine_config, timeline: _Timeline) -> None:
        self.config = engine_config
        self.timeline = timeline
        self.kwargs: dict[str, Any] = {}

    async def _finalize(self) -> None:
        await asyncio.sleep(SLOW_FINISH_S)
        self.timeline.mark("finish_end")

    async def execute(self, **kwargs: Any) -> AgentRun:
        self.kwargs = kwargs
        on_content = kwargs.get("on_content")
        if on_content:
            await on_content("The answer is 42.")
        run = AgentRun(
            id="run-early-1",
            status=RunStatus.COMPLETED,
            output_text="The answer is 42.",
            trigger_type=kwargs.get("trigger_type", TriggerType.WEBCHAT),
            duration_ms=1234,
        )
        on_complete = kwargs.get("on_complete")
        if on_complete is not None:
            await on_complete(run)
            get_task_registry().spawn(self._finalize(), name="finalize-run:test")
            return run
        await self._finalize()
        return run


async def _stream_chat(app, body: dict[str, Any], timeline: _Timeline) -> list[dict[str, Any]]:
    """Drive the ASGI app directly, timestamping each SSE event as it is SENT.

    httpx's ASGI transport buffers the whole body, which would hide the very
    ordering under test.
    """
    events: list[dict[str, Any]] = []
    payload = json.dumps(body).encode()
    delivered = False
    never = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] != "http.response.body":
            return
        chunk = message.get("body", b"").decode()
        for block in chunk.split("\n\n"):
            if not block.startswith("event:"):
                continue
            lines = block.split("\n")
            name = lines[0].removeprefix("event: ").strip()
            data = json.loads(lines[1].removeprefix("data: "))
            timeline.mark(f"sse_{name}")
            events.append({"event": name, "data": data})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/send",
        "raw_path": b"/chat/send",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return events


@pytest.fixture
def chat_app_with(engine_config):
    from fastapi import FastAPI

    from robothor.engine.chat import _sessions, init_chat, router

    def build(runner: Any) -> Any:
        _sessions.clear()
        app = FastAPI()
        with patch("robothor.engine.chat.load_all_sessions", return_value={}):
            init_chat(runner, engine_config)
        app.include_router(router)
        return app

    yield build

    _sessions.clear()


class TestWebchatDoneEvent:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_done_arrives_before_slow_finalization(self, engine_config, chat_app_with):
        timeline = _Timeline()
        runner = _ContractRunner(engine_config, timeline)
        app = chat_app_with(runner)

        with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
            events = await _stream_chat(
                app, {"session_key": "test:main:web", "message": "hi"}, timeline
            )
            assert "finish_end" not in timeline.marks
            await get_task_registry().drain(timeout=5.0)

        done = [e for e in events if e["event"] == "done"]
        assert len(done) == 1
        assert done[0]["data"]["text"] == "The answer is 42."
        assert done[0]["data"]["run_id"] == "run-early-1"
        assert done[0]["data"]["duration_ms"] == 1234
        assert timeline.marks["sse_done"] < timeline.marks["finish_end"]
        assert callable(runner.kwargs.get("on_complete"))

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_off")
    async def test_flag_off_done_arrives_after_finalization(self, engine_config, chat_app_with):
        timeline = _Timeline()
        runner = _ContractRunner(engine_config, timeline)
        app = chat_app_with(runner)

        with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
            events = await _stream_chat(
                app, {"session_key": "test:main:web", "message": "hi"}, timeline
            )

        done = [e for e in events if e["event"] == "done"]
        assert len(done) == 1
        assert done[0]["data"]["text"] == "The answer is 42."
        assert timeline.marks["finish_end"] < timeline.marks["sse_done"]
        assert runner.kwargs.get("on_complete") is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_history_and_save_still_happen_once(self, engine_config, chat_app_with):
        from robothor.engine.chat import _get_session

        timeline = _Timeline()
        runner = _ContractRunner(engine_config, timeline)
        app = chat_app_with(runner)
        save = AsyncMock()

        with patch("robothor.engine.chat.save_exchange_async", new=save):
            events = await _stream_chat(
                app, {"session_key": "test:main:web", "message": "hi"}, timeline
            )
            await get_task_registry().drain(timeout=5.0)

        assert [e["event"] for e in events].count("done") == 1
        history = _get_session("test:main:web").history
        assert history[-1] == {"role": "assistant", "content": "The answer is 42."}
        save.assert_awaited_once()


# ── Telegram ─────────────────────────────────────────────────────────────


@pytest.fixture
def telegram_bot(engine_config):
    from robothor.engine.chat import _sessions
    from robothor.engine.telegram import TelegramBot

    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
        patch("robothor.engine.delivery._persist_delivery_status", new_callable=AsyncMock),
        patch("robothor.engine.telegram.save_exchange_async", new=AsyncMock(return_value=None)),
    ):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.delete_message = AsyncMock()
        mock_bot.send_chat_action = AsyncMock()
        mock_bot_cls.return_value = mock_bot
        tg = TelegramBot(engine_config, MagicMock())
        tg.bot = mock_bot
        yield tg
    _sessions.clear()


async def _telegram_turn(bot, runner: _ContractRunner, timeline: _Timeline) -> list[str]:
    from robothor.engine.chat import get_shared_session

    sent: list[str] = []

    async def send_message(chat_id: str, text: str, *args: Any, **kwargs: Any) -> list[Any]:
        timeline.mark("telegram_sent")
        sent.append(text)
        return [MagicMock(message_id=777)]

    bot.runner = runner
    bot.send_message = send_message
    session_key = bot._session_key("12345")
    session = get_shared_session(session_key)
    await bot._run_interactive("12345", session_key, session, "hello")
    task = bot._active_tasks.get("12345")
    if task:
        await task
    return sent


class TestTelegramAnswer:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_answer_sent_before_slow_finalization(self, engine_config, telegram_bot):
        timeline = _Timeline()
        runner = _ContractRunner(engine_config, timeline)

        sent = await _telegram_turn(telegram_bot, runner, timeline)
        assert "finish_end" not in timeline.marks
        await get_task_registry().drain(timeout=5.0)

        assert sent == ["The answer is 42."]
        assert timeline.marks["telegram_sent"] < timeline.marks["finish_end"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_off")
    async def test_flag_off_answer_sent_after_finalization(self, engine_config, telegram_bot):
        timeline = _Timeline()
        runner = _ContractRunner(engine_config, timeline)

        sent = await _telegram_turn(telegram_bot, runner, timeline)

        assert sent == ["The answer is 42."]
        assert timeline.marks["finish_end"] < timeline.marks["telegram_sent"]
        assert runner.kwargs.get("on_complete") is None
