"""Steer, stop and progress for a conversation's live run, on any channel.

The engine has always been able to steer or halt a run by ``run_id``
(``interrupt_api``). A chat surface does not know the run id; it knows the
conversation. These tests pin the conversation-to-run lookup, the boundary at
which a steer and a halt land, and the channel-neutral progress line.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from robothor.engine import live_control, session_registry
from robothor.engine.loop_guards import GuardState, check_iteration_guards
from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.request_runtime import observe_request
from robothor.engine.session import AgentSession

KEY = "agent:main:slack:C123"
TENANT = "tenant-a"


@pytest.fixture(autouse=True)
def _clear_registry():
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)
    yield
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)


def _session(tenant: str = TENANT) -> AgentSession:
    session = AgentSession("main", TriggerType.SLACK, None, tenant)
    session.start("system", "draft the report", [])
    return session


def _boundary(session: AgentSession) -> bool:
    """One pass of the loop's iteration guards: True means the loop stops."""
    return check_iteration_guards(
        session,
        SimpleNamespace(id="main"),
        watchdog=None,
        wallclock_deadline=None,
        wallclock_ceiling=0,
        state=GuardState(),
    )


def _steering_turns(session: AgentSession) -> list[str]:
    return [
        m["content"]
        for m in session.messages
        if m["role"] == "user" and str(m["content"]).startswith("[operator steering update]")
    ]


class TestConversationLookup:
    @pytest.mark.asyncio
    async def test_a_run_started_in_the_scope_is_found_by_its_conversation(self):
        session = _session()
        with live_control.conversation_scope(KEY):
            async with observe_request(session, None):
                assert live_control.find_live_run(KEY, tenant_id=TENANT) == session.run_id
        assert live_control.find_live_run(KEY, tenant_id=TENANT) is None

    @pytest.mark.asyncio
    async def test_a_run_outside_any_scope_belongs_to_no_conversation(self):
        session = _session()
        async with observe_request(session, None):
            assert live_control.find_live_run(KEY, tenant_id=TENANT) is None

    @pytest.mark.asyncio
    async def test_a_sub_agent_started_inside_the_run_is_not_the_conversations_run(self):
        parent, child = _session(), _session()
        with live_control.conversation_scope(KEY):
            async with observe_request(parent, None):
                async with observe_request(child, None):
                    assert live_control.find_live_run(KEY, tenant_id=TENANT) == parent.run_id

    @pytest.mark.asyncio
    async def test_a_verification_retry_keeps_the_runs_conversation(self):
        """run_lifecycle enters observe_request a second time on the same session."""
        session = _session()
        with live_control.conversation_scope(KEY):
            async with observe_request(session, None):
                pass
            async with observe_request(session, None):
                assert live_control.find_live_run(KEY, tenant_id=TENANT) == session.run_id

    @pytest.mark.asyncio
    async def test_another_tenants_run_is_never_found(self):
        session = _session(tenant="tenant-b")
        with live_control.conversation_scope(KEY):
            async with observe_request(session, None):
                assert live_control.find_live_run(KEY, tenant_id=TENANT) is None
                assert live_control.steer_conversation(KEY, "x", tenant_id=TENANT) is False
                assert live_control.interrupt_conversation(KEY, tenant_id=TENANT) is False
        assert _steering_turns(session) == []
        assert session.consume_interrupt() is None


class TestSteerAndStopLandAtTheBoundary:
    @pytest.mark.asyncio
    async def test_a_steer_during_a_run_is_consumed_at_the_next_iteration_boundary(self):
        session = _session()
        steered = asyncio.Event()
        boundary_results: list[bool] = []

        async def run() -> None:
            with live_control.conversation_scope(KEY):
                async with observe_request(session, None):
                    await steered.wait()
                    assert _steering_turns(session) == [], "nothing lands before the boundary"
                    boundary_results.append(_boundary(session))

        task = asyncio.create_task(run())
        while live_control.find_live_run(KEY, tenant_id=TENANT) is None:
            await asyncio.sleep(0)
        assert live_control.steer_conversation(KEY, "use the EU region", tenant_id=TENANT)
        steered.set()
        await task

        assert boundary_results == [False], "a steer never stops the run"
        assert _steering_turns(session) == ["[operator steering update]\nuse the EU region"]

    @pytest.mark.asyncio
    async def test_an_interrupt_ends_the_run_cancelled_not_failed(self):
        session = _session()
        with live_control.conversation_scope(KEY):
            async with observe_request(session, None):
                assert live_control.interrupt_conversation(KEY, tenant_id=TENANT)
                assert _boundary(session) is True
        assert session.was_interrupted
        run = session.cancelled(session._interrupt_note)
        assert run.status == RunStatus.CANCELLED

    def test_nothing_running_means_nothing_found(self):
        assert live_control.steer_conversation(KEY, "x", tenant_id=TENANT) is False
        assert live_control.interrupt_conversation(KEY, tenant_id=TENANT) is False

    def test_an_empty_steer_is_refused(self):
        assert live_control.steer_conversation(KEY, "   ", tenant_id=TENANT) is False


class TestProgressLine:
    def test_tool_start_names_the_tool_and_the_elapsed_time(self):
        line = live_control.format_progress_line(
            {"event": "tool_start", "tool": "web_search", "args": {"q": "secret query"}},
            elapsed_s=4.2,
        )
        assert line is not None
        assert "web search" in line.lower()
        assert "4s" in line
        assert "secret query" not in line

    def test_tool_end_gives_the_duration_and_marks_a_failure(self):
        ok = live_control.format_progress_line(
            {
                "event": "tool_end",
                "tool": "read_file",
                "duration_ms": 1500,
                "error": None,
                "result_preview": "TOP SECRET CONTENTS",
            },
            elapsed_s=9,
        )
        failed = live_control.format_progress_line(
            {"event": "tool_end", "tool": "read_file", "duration_ms": 200, "error": "boom"},
            elapsed_s=10,
        )
        assert ok is not None and failed is not None
        assert "1.5s" in ok and "TOP SECRET" not in ok
        assert ok != failed
        assert "failed" in failed.lower()

    def test_iteration_start_is_a_step_line(self):
        line = live_control.format_progress_line(
            {"event": "iteration_start", "iteration": 3}, elapsed_s=12
        )
        assert line is not None and "Step 3" in line and "12s" in line

    def test_other_events_give_no_line(self):
        assert live_control.format_progress_line({"event": "delta"}, elapsed_s=1) is None

    def test_a_line_is_compact_however_long_the_tool_name(self):
        line = live_control.format_progress_line(
            {"event": "tool_start", "tool": "x" * 500}, elapsed_s=1
        )
        assert line is not None and len(line) <= 120 and "\n" not in line


class _FakeChannel:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.edits: list[tuple[str, str]] = []

    async def post(self, text: str) -> str:
        self.posts.append(text)
        return "msg-1"

    async def edit(self, handle: str, text: str) -> None:
        self.edits.append((handle, text))


class TestProgressMessage:
    @pytest.mark.asyncio
    async def test_a_burst_is_one_post_and_at_most_one_edit_a_second(self):
        fake = _FakeChannel()
        progress = live_control.ProgressMessage(fake.post, fake.edit, min_interval=1.0)
        for i in range(50):
            await progress.on_event({"event": "tool_start", "tool": f"tool_{i}"})
        await asyncio.sleep(0.2)
        assert len(fake.posts) == 1
        assert len(fake.posts) + len(fake.edits) <= 2
        await progress.close()
        written = fake.posts + [text for _, text in fake.edits]
        assert "tool 49" in written[-1], "the latest state is what the channel shows"

    @pytest.mark.asyncio
    async def test_close_with_a_final_line_writes_it(self):
        fake = _FakeChannel()
        progress = live_control.ProgressMessage(fake.post, fake.edit, min_interval=1.0)
        await progress.on_event({"event": "iteration_start", "iteration": 1})
        await asyncio.sleep(0.05)
        await progress.close("Done")
        assert fake.edits[-1] == ("msg-1", "Done")

    @pytest.mark.asyncio
    async def test_a_close_during_the_first_post_still_finishes_the_message(self):
        """The run ended while the first post was on its way: it must still read Done."""
        fake = _FakeChannel()
        landed = asyncio.Event()

        async def slow_post(text: str) -> str:
            await landed.wait()
            return await fake.post(text)

        progress = live_control.ProgressMessage(slow_post, fake.edit, min_interval=1.0)
        await progress.on_event({"event": "tool_start", "tool": "web_search"})
        await asyncio.sleep(0.01)
        closing = asyncio.create_task(progress.close("Done"))
        await asyncio.sleep(0.01)
        landed.set()
        await asyncio.wait_for(closing, timeout=5)
        assert len(fake.posts) == 1
        assert fake.edits == [("msg-1", "Done")]

    @pytest.mark.asyncio
    async def test_no_event_means_nothing_is_ever_posted(self):
        fake = _FakeChannel()
        progress = live_control.ProgressMessage(fake.post, fake.edit, min_interval=1.0)
        await progress.close("Done")
        assert fake.posts == [] and fake.edits == []

    @pytest.mark.asyncio
    async def test_a_channel_failure_never_reaches_the_run(self):
        async def broken_post(text: str) -> str:
            raise RuntimeError("platform down")

        async def edit(handle: str, text: str) -> None:  # pragma: no cover - never reached
            raise AssertionError

        progress = live_control.ProgressMessage(broken_post, edit, min_interval=0.01)
        await progress.on_event({"event": "tool_start", "tool": "web_search"})
        await asyncio.sleep(0.05)
        await progress.close("Done")


def test_the_flag_is_off_by_default():
    from robothor.settings.model import GenusSettings

    assert GenusSettings().channels.steer_everywhere is False


class TestASteerDuringTheFinalAnswer:
    """The model answered without tools while the steer was on its way."""

    @pytest.mark.asyncio
    async def test_the_run_takes_another_turn_with_the_steer(self):
        from robothor.engine.live_inbox import keep_going_after_answer

        session = _session()  # a Slack-style run: no live inbox
        session.messages.append({"role": "assistant", "content": "Here is the report."})
        session.steer("use the EU region")
        assert await keep_going_after_answer(session, None, "Here is the report.") is True
        assert _steering_turns(session) == ["[operator steering update]\nuse the EU region"]

    @pytest.mark.asyncio
    async def test_a_webchat_run_shows_the_superseded_answer_as_interim(self):
        from robothor.engine.live_inbox import LiveInbox, keep_going_after_answer

        session = _session()
        inbox = LiveInbox()
        shown: list[str] = []

        async def on_interim(text: str) -> None:
            shown.append(text)

        inbox.on_interim = on_interim
        session.live_inbox = inbox
        session.messages.append({"role": "assistant", "content": "Here is the report."})
        session.steer("use the EU region")
        assert await keep_going_after_answer(session, None, "Here is the report.") is True
        assert shown == ["Here is the report."]

    @pytest.mark.asyncio
    async def test_no_steer_means_the_answer_stands(self):
        from robothor.engine.live_inbox import keep_going_after_answer

        session = _session()
        session.messages.append({"role": "assistant", "content": "Here is the report."})
        assert await keep_going_after_answer(session, None, "Here is the report.") is False
