"""A chat message sent while a run is working joins THAT run.

Before this, a second Telegram message waited behind the run and became its own
turn once the run finished: the agent answered twice, and the first answer was
written without the context the operator had just added. The run now drains a
per-conversation ``LiveInbox`` at its safe points — after a tool turn's results
are recorded, and when the model is about to stop — and carries on.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.live_inbox import (
    FOLLOWUP_PIN,
    MAX_LATE_EXTENSIONS,
    LiveInbox,
    absorb_followups,
    absorb_late_followups,
    history_text,
)
from robothor.engine.models import RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession

# ── The mailbox ──────────────────────────────────────────────────────────


class TestLiveInbox:
    def test_push_then_drain_in_order(self) -> None:
        box = LiveInbox()
        assert box.push("one") is True
        assert box.push("two") is True
        assert [i.text for i in box.take()] == ["one", "two"]
        assert box.take() == []

    def test_close_returns_leftovers_and_refuses_later_pushes(self) -> None:
        box = LiveInbox()
        box.push("late")
        assert [i.text for i in box.close()] == ["late"]
        assert box.is_open is False
        assert box.push("after") is False
        assert box.close() == []

    def test_blank_text_is_not_a_followup(self) -> None:
        box = LiveInbox()
        assert box.push("   ") is False
        assert len(box) == 0

    def test_meta_travels_with_the_item(self) -> None:
        box = LiveInbox()
        box.push("see file", {"message_id": 7})
        assert box.take()[0].meta == {"message_id": 7}


# ── The session side ─────────────────────────────────────────────────────


def _session_with(box: LiveInbox) -> AgentSession:
    s = AgentSession(agent_id="a")
    s.start("system prompt", "do the thing", [])
    s.live_inbox = box
    return s


class TestAbsorb:
    def test_followup_becomes_a_pinned_user_turn(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        box.push("also include Y")
        assert absorb_followups(s) is True
        last = s.messages[-1]
        assert last["role"] == "user"
        assert "also include Y" in last["content"]
        assert last["_pin"] == FOLLOWUP_PIN
        assert [i.text for i in box.taken] == ["also include Y"]

    def test_followup_lands_in_the_task_record(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        box.push("use the Q3 numbers")
        absorb_followups(s)
        assert "use the Q3 numbers" in s.messages[0]["content"]

    def test_nothing_pending_changes_nothing(self) -> None:
        s = _session_with(LiveInbox())
        before = list(s.messages)
        assert absorb_followups(s) is False
        assert s.messages == before

    def test_no_inbox_is_a_noop(self) -> None:
        s = AgentSession(agent_id="a")
        s.start("sys", "hi", [])
        assert absorb_followups(s) is False

    def test_pending_inbox_counts_as_pending_control(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        assert s.has_pending_control is False
        box.push("wait")
        assert s.has_pending_control is True

    def test_late_extensions_are_capped(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        for n in range(MAX_LATE_EXTENSIONS):
            box.push(f"more {n}")
            assert absorb_late_followups(s) is True
        box.push("one too many")
        assert absorb_late_followups(s) is False
        # Not dropped: it stays for the channel's follow-up turn.
        assert [i.text for i in box.close()] == ["one too many"]

    def test_history_text_folds_followups_into_the_user_row(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        box.push("and Y")
        absorb_followups(s)
        text = history_text("do X", box.taken)
        assert text.startswith("do X")
        assert "and Y" in text
        assert history_text("do X", []) == "do X"


def test_a_card_typed_mid_run_gets_the_same_backstop_as_the_request() -> None:
    """`session.start` masks a PAN a person typed; a follow-up is typed too."""
    from robothor.engine.models import TriggerType

    box = LiveInbox()
    s = AgentSession(agent_id="a", trigger_type=TriggerType.TELEGRAM)
    s.start("system prompt", "book it", [])
    s.live_inbox = box
    box.push("use card 4111 1111 1111 1111")
    absorb_followups(s)
    assert "4111 1111 1111 1111" not in str(s.messages[-1]["content"])
    assert "4111 1111 1111 1111" not in str(s.messages[0]["content"])  # task record


def test_a_deliverable_named_mid_run_is_part_of_the_task() -> None:
    """ "Also save it to report.md" must reach the deliverable contract."""
    from robothor.engine.deliverable_contract import task_text_for_run

    box = LiveInbox()
    s = _session_with(box)
    box.push("also save it to /tmp/report.md")
    absorb_followups(s)
    text = task_text_for_run(s.run, s)
    assert "do the thing" in text
    assert "/tmp/report.md" in text


@pytest.mark.asyncio
async def test_a_superseded_answer_is_never_the_final_answer() -> None:
    """Review #1: it was already sent as an interim. If the run then ends with
    no newer text (budget, interrupt, a reasoning-only turn), reporting it again
    as the result would answer the follow-up with the answer it superseded."""
    from robothor.engine.live_inbox import extend_for_late_followups

    box = LiveInbox()
    s = _session_with(box)
    s.messages.append({"role": "assistant", "content": "the forecast"})
    box.push("and the tides?")
    assert await extend_for_late_followups(s, "the forecast") is True
    assert s.get_final_text() is None


@pytest.mark.asyncio
async def test_a_block_list_answer_is_delivered_as_its_text() -> None:
    """Review #4: extended-thinking content is a list of blocks, not a str."""
    from robothor.engine.live_inbox import extend_for_late_followups

    sent: list[str] = []

    async def on_interim(text: str) -> None:
        sent.append(text)

    box = LiveInbox(on_interim=on_interim)
    s = _session_with(box)
    blocks = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "the forecast"}]
    s.messages.append({"role": "assistant", "content": blocks})
    box.push("and the tides?")
    assert await extend_for_late_followups(s, blocks) is True
    assert sent == ["the forecast"]


class TestFollowupsWaitForTheRunToGoOn:
    """Review #3: taking a follow-up and then stopping answered nobody, and
    `close()` could not hand it back because it had been taken."""

    def _guards(self, s):
        from robothor.engine.loop_guards import GuardState, check_iteration_guards

        return check_iteration_guards(
            s,
            MagicMock(),
            watchdog=None,
            wallclock_deadline=None,
            wallclock_ceiling=0,
            state=GuardState(),
        )

    def test_an_interrupted_run_leaves_them_for_the_channel(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        box.push("also Y")
        s.interrupt("halt")
        assert self._guards(s) is True
        assert [i.text for i in box.close()] == ["also Y"]

    def test_a_confirmed_routine_operation_leaves_them_for_the_channel(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        s.routine_operation_bound = True
        box.push("also Y")
        with patch("robothor.engine.loop_guards._runaway", return_value=False):
            self._guards(s)
        assert [i.text for i in box.close()] == ["also Y"]

    def test_a_run_that_goes_on_takes_them(self) -> None:
        box = LiveInbox()
        s = _session_with(box)
        box.push("also Y")
        with patch("robothor.engine.loop_guards._runaway", return_value=False):
            assert self._guards(s) is False
        assert [i.text for i in box.taken] == ["also Y"]


def test_compaction_keeps_a_followup_with_the_request() -> None:
    """It amends the request; summarising it away would undo what was asked."""
    from robothor.engine.compaction import _split_for_summary

    box = LiveInbox()
    s = _session_with(box)
    for n in range(30):
        s.messages.append({"role": "assistant", "content": f"working {n}"})
        s.messages.append({"role": "user", "content": f"ok {n}"})
    box.push("switch to the Q3 numbers")
    absorb_followups(s)
    s.messages.extend({"role": "assistant", "content": f"more {n}"} for n in range(5))

    head, _retained, _rest = _split_for_summary(s.messages)
    assert any("switch to the Q3 numbers" in str(m.get("content")) for m in head[1:])


def test_only_one_consumer_drains_operator_steers() -> None:
    """The loop top and `_after_iteration` both drained steers under two labels."""
    from robothor.engine.run_lifecycle import RunLifecycleMixin

    s = AgentSession(agent_id="a")
    s.start("sys", "hi", [])
    s.steer("focus")
    before = len(s.messages)
    asyncio.run(RunLifecycleMixin._after_iteration(MagicMock(), s, 1))
    assert len(s.messages) == before
    assert s.consume_pending_steer() == "focus"


# ── The loop ─────────────────────────────────────────────────────────────


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        registry.get_tool_names.return_value = ["list_tasks"]
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _tool_call_response(mock_litellm_response):
    tc = MagicMock()
    tc.id = "call_1"
    tc.function.name = "list_tasks"
    tc.function.arguments = json.dumps({})
    response = mock_litellm_response(content=None, tool_calls=[tc])
    response.choices[0].message.content = None
    return response


async def _execute(runner, config, completion, **kwargs):
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        return await runner.execute("test-agent", "List my tasks", agent_config=config, **kwargs)


@pytest.mark.asyncio
async def test_followup_during_a_tool_joins_after_its_result(
    runner, sample_agent_config, mock_litellm_response
):
    box = LiveInbox()
    seen: list[list[dict]] = []

    async def tool(*_a, **_k):
        box.push("only the overdue ones")
        return {"tasks": [], "count": 0}

    runner.registry.execute = AsyncMock(side_effect=tool)
    responses = [
        _tool_call_response(mock_litellm_response),
        mock_litellm_response(content="No overdue tasks."),
    ]

    async def completion(**kwargs):
        seen.append(kwargs["messages"])
        return responses[len(seen) - 1]

    run = await _execute(runner, sample_agent_config, completion, live_inbox=box)

    assert run.status == RunStatus.COMPLETED
    assert run.output_text == "No overdue tasks."
    second = seen[1]
    roles = [m["role"] for m in second]
    tool_at = roles.index("tool")
    # The text is also in the system message's task record (that is how it
    # survives compaction); the TURN is the user message carrying it.
    followup_at = next(
        i
        for i, m in enumerate(second)
        if m["role"] == "user" and "only the overdue ones" in str(m.get("content"))
    )
    assert followup_at > tool_at
    assert all("_pin" not in m for m in second)


@pytest.mark.asyncio
async def test_followup_during_the_final_answer_extends_the_run(
    runner, sample_agent_config, mock_litellm_response
):
    interim: list[str] = []

    async def on_interim(text: str) -> None:
        interim.append(text)

    box = LiveInbox(on_interim=on_interim)
    seen: list[list[dict]] = []
    responses = [
        mock_litellm_response(content="Here is the draft."),
        mock_litellm_response(content="Here is the draft, now with Y."),
    ]

    async def completion(**kwargs):
        seen.append(kwargs["messages"])
        if len(seen) == 1:
            box.push("also include Y")
        return responses[len(seen) - 1]

    run = await _execute(runner, sample_agent_config, completion, live_inbox=box)

    assert run.status == RunStatus.COMPLETED
    assert len(seen) == 2
    assert run.output_text == "Here is the draft, now with Y."
    assert interim == ["Here is the draft."]
    assert any("also include Y" in str(m.get("content")) for m in seen[1])
    assert box.close() == []


@pytest.mark.asyncio
async def test_without_an_inbox_the_run_is_unchanged(
    runner, sample_agent_config, mock_litellm_response
):
    async def completion(**_kwargs):
        return mock_litellm_response(content="done")

    run = await _execute(runner, sample_agent_config, completion)
    assert run.output_text == "done"
