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
        # Before finalization even started, not merely before it ended.
        assert timeline.marks["on_complete"] < timeline.marks["finish_start"]

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


# ── Execution lanes ──────────────────────────────────────────────────────


def _fake_lanes_module():
    """A stand-in for ``robothor.engine.lanes``: a Lane enum and ``in_lane``."""
    import contextlib
    import contextvars
    import enum
    import types

    lane_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("lane", default=None)

    class Lane(enum.StrEnum):
        INTERACTIVE = "interactive"
        BACKGROUND = "background"

    @contextlib.contextmanager
    def in_lane(lane):
        token = lane_var.set(lane)
        try:
            yield
        finally:
            lane_var.reset(token)

    module = types.ModuleType("robothor.engine.lanes")
    module.Lane = Lane
    module.in_lane = in_lane
    module.lane_var = lane_var
    return module


class _LaneRecordingRunner:
    def __init__(self, engine_config, lane_var=None) -> None:
        self.config = engine_config
        self.lane_var = lane_var
        self.finished_in: list[Any] = []

    def _publish_run_telemetry(self, trace, run) -> None:
        return None

    def _finish_run(self, run, **kwargs):
        self.finished_in.append(self.lane_var.get() if self.lane_var else "no-lanes")
        return run

    async def _persist_run(self, run) -> None:
        return None


class TestFinalizationRunsInTheBackgroundLane:
    @pytest.mark.asyncio
    async def test_finalization_is_spawned_in_the_background_lane(self, engine_config, monkeypatch):
        import sys

        from robothor.engine.early_done import release_then_finalize

        lanes = _fake_lanes_module()
        monkeypatch.setitem(sys.modules, "robothor.engine.lanes", lanes)
        runner = _LaneRecordingRunner(engine_config, lanes.lane_var)
        run = AgentRun(id="run-lane-1", status=RunStatus.COMPLETED, output_text="ok")
        seen_by_caller: list[Any] = []

        async def on_complete(r: AgentRun) -> None:
            seen_by_caller.append(lanes.lane_var.get())

        with lanes.in_lane(lanes.Lane.INTERACTIVE):
            await release_then_finalize(runner, run, None, on_complete)
            # The caller's own context is untouched by the background spawn.
            assert lanes.lane_var.get() == lanes.Lane.INTERACTIVE
        await get_task_registry().drain(timeout=5.0)

        assert seen_by_caller == [lanes.Lane.INTERACTIVE]
        assert runner.finished_in == [lanes.Lane.BACKGROUND]

    @pytest.mark.asyncio
    async def test_without_a_lanes_module_finalization_still_runs(self, engine_config, monkeypatch):
        import sys

        from robothor.engine.early_done import release_then_finalize

        monkeypatch.setitem(sys.modules, "robothor.engine.lanes", None)  # import fails
        runner = _LaneRecordingRunner(engine_config)
        run = AgentRun(id="run-lane-2", status=RunStatus.COMPLETED, output_text="ok")

        await release_then_finalize(runner, run, None, AsyncMock())
        await get_task_registry().drain(timeout=5.0)

        assert runner.finished_in == ["no-lanes"]


# ── Review follow-ups ────────────────────────────────────────────────────


class _BlockingReleaseRunner:
    """Uses the real ``release_then_finalize`` with a BLOCKING ``_finish_run``.

    Records whether the live inbox was still open when the answer went out.
    """

    def __init__(self, engine_config, timeline: _Timeline) -> None:
        self.config = engine_config
        self.timeline = timeline
        self.kwargs: dict[str, Any] = {}
        self.inbox_open_after_release: bool | None = None

    def _publish_run_telemetry(self, trace, run) -> None:
        return None

    def _finish_run(self, run, **kwargs):
        self.timeline.mark("finish_start")
        time.sleep(SLOW_FINISH_S)  # blocks the loop, as _finish_run does today
        self.timeline.mark("finish_end")
        return run

    async def _persist_run(self, run) -> None:
        return None

    async def execute(self, **kwargs: Any) -> AgentRun:
        from robothor.engine.early_done import release_then_finalize

        self.kwargs = kwargs
        run = AgentRun(
            id="run-early-2",
            status=RunStatus.COMPLETED,
            output_text="The answer is 42.",
            trigger_type=kwargs.get("trigger_type", TriggerType.WEBCHAT),
            duration_ms=1234,
        )
        inbox = kwargs.get("live_inbox")

        async def on_complete(r: AgentRun) -> None:
            await kwargs["on_complete"](r)
            if inbox is not None:
                self.inbox_open_after_release = inbox.is_open

        return await release_then_finalize(self, run, None, on_complete)


class TestReviewFollowUps:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_done_reaches_the_socket_through_http_middleware_before_finalization(
        self, engine_config, chat_app_with
    ):
        """The engine app wraps every route in an ``@app.middleware("http")``,
        which adds a task hop per streamed chunk; ``done`` must still be sent
        before the blocking finalization starts."""
        timeline = _Timeline()
        runner = _BlockingReleaseRunner(engine_config, timeline)
        app = chat_app_with(runner)

        @app.middleware("http")
        async def passthrough(request, call_next):
            return await call_next(request)

        with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
            await _stream_chat(app, {"session_key": "test:main:web", "message": "hi"}, timeline)
            await get_task_registry().drain(timeout=5.0)

        assert timeline.marks["sse_done"] < timeline.marks["finish_start"], timeline.marks

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_webchat_seals_the_live_inbox_when_it_releases(
        self, engine_config, chat_app_with
    ):
        timeline = _Timeline()
        runner = _BlockingReleaseRunner(engine_config, timeline)
        app = chat_app_with(runner)

        with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
            await _stream_chat(app, {"session_key": "test:main:web", "message": "hi"}, timeline)
            await get_task_registry().drain(timeout=5.0)

        assert runner.inbox_open_after_release is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on")
    async def test_telegram_seals_the_live_inbox_before_sending(self, engine_config, telegram_bot):
        from robothor.engine.chat import get_shared_session

        timeline = _Timeline()
        runner = _BlockingReleaseRunner(engine_config, timeline)
        open_at_send: list[bool] = []

        async def send_message(chat_id: str, text: str, *args: Any, **kwargs: Any) -> list[Any]:
            open_at_send.append(runner.kwargs["live_inbox"].is_open)
            return [MagicMock(message_id=777)]

        telegram_bot.runner = runner
        telegram_bot.send_message = send_message
        session_key = telegram_bot._session_key("12345")
        await telegram_bot._run_interactive(
            "12345", session_key, get_shared_session(session_key), "hello"
        )
        task = telegram_bot._active_tasks.get("12345")
        if task:
            await task
        await get_task_registry().drain(timeout=5.0)

        assert open_at_send == [False]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("early_done_on", "_mock_run_persistence")
    async def test_a_raising_output_policy_still_releases_and_finalizes(
        self, runner, agent_config, caplog
    ):
        timeline = _Timeline()
        on_complete = AsyncMock()

        with (
            patch(
                "robothor.engine.deliverable_verdict.record_deliverable_verdicts",
                side_effect=RuntimeError("policy bug"),
            ),
            patch.object(AgentRunner, "_finish_run", _slow_finish(timeline)),
        ):
            run = await _execute(runner, agent_config, on_complete=on_complete)
            await get_task_registry().drain(timeout=5.0)

        on_complete.assert_awaited_once_with(run)
        assert "finish_end" in timeline.marks
        assert any("policy bug" in r.getMessage() for r in caplog.records)


# ── Telemetry sees the final status ──────────────────────────────────────


@pytest.mark.usefixtures("_mock_run_persistence")
class TestTelemetryIsPublishedWithTheFinalStatus:
    """Publishing a run's telemetry closes its trace; it must not happen while
    the run still reads ``running``. Finalization publishes once the status is
    final, so the success path makes no publish of its own before that."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["1", ""])
    async def test_every_publish_sees_a_terminal_status(
        self, runner, agent_config, monkeypatch, flag
    ):
        monkeypatch.setenv("ROBOTHOR_EARLY_DONE", flag)
        statuses: list[RunStatus] = []

        def spy(self, trace, run) -> None:
            statuses.append(run.status)

        with patch.object(AgentRunner, "_publish_run_telemetry", spy):
            run = await _execute(runner, agent_config, on_complete=AsyncMock())
            await get_task_registry().drain(timeout=5.0)

        assert run.status == RunStatus.COMPLETED
        assert RunStatus.RUNNING not in statuses, statuses
