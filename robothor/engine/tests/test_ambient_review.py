"""Ambient follow-ups: the enforce-path defects review found.

Each test here pins a way the first version could send, or claim to send,
something the manifest's tiers did not allow, or miss what it exists to find.
The fakes in ``test_ambient_worker.py`` stand in for the seams; these reach the
real implementations behind them wherever a unit test can.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.ambient import (
    PROPOSAL_STEP,
    AmbientConfig,
    AmbientWorker,
    Message,
    Thread,
    _inbox_threads,
    composing_config,
)
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _agent(agent_id: str = "example-agent", **tiers: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        name="Example Agent",
        delivery_channel="telegram",
        delivery_to="12345",
        ambient=AmbientConfig(
            watch=["inbox"],
            stalled_after="4h",
            stalled_after_seconds=4 * 3600,
            digest_at="0 9 * * *",
            tiers={"nudge_thread": "approve", "send_digest": "never", "reply_on_behalf": "never"}
            | tiers,
        ),
    )


def _thread(thread_id: str = "12") -> Thread:
    return Thread(
        source="inbox",
        thread_id=thread_id,
        person="Alice",
        messages=[Message(direction="inbound", at=datetime(2026, 10, 31, 6, 0, tzinfo=UTC))],
    )


class _Runner:
    def __init__(self, output: str, status: RunStatus = RunStatus.COMPLETED) -> None:
        self.output, self.status, self.calls = output, status, []

    async def execute(self, **kwargs: Any) -> AgentRun:
        self.calls.append(kwargs)
        return AgentRun(
            agent_id=kwargs["agent_id"],
            trigger_type=TriggerType.CRON,
            status=self.status,
            output_text=self.output,
        )


class _Channel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []

    async def send(self, target: str, text: str, **kw: Any) -> Any:
        from robothor.engine.channels.base import SendReceipt

        self.sends.append((target, text))
        return SendReceipt(acknowledged=1, expected=1)


def _worker(runner: Any, channel: _Channel, *, threads=None, approver=None, steps=None, **kw):
    async def load(agent: AgentConfig) -> list[Thread]:
        return threads if threads is not None else [_thread()]

    async def record(new: list[Any]) -> None:
        if steps is not None:
            steps.extend(new)

    return AmbientWorker(
        runner,
        load_threads=load,
        channel_resolver=lambda name: channel,
        approver=lambda: approver,
        record_steps=record,
        now=lambda: NOW,
        **kw,
    )


@pytest.fixture
def enforce(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "enforce")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))


# ── reply_on_behalf must not claim a send it cannot make ──────────────


async def test_reply_on_behalf_is_not_written_to_the_crm_log_or_called_sent(enforce, tmp_path):
    """The CRM inbox is a log of what happened elsewhere; a row there reaches nobody."""
    runner = _Runner("### reply_on_behalf inbox:12\nHi Alice, we are on it.\n")
    channel = _Channel()
    steps: list[Any] = []
    worker = _worker(
        runner,
        channel,
        steps=steps,
        digest_dir=tmp_path,
    )
    with patch("robothor.crm.dal.send_message") as crm_write:
        result = await worker.run_pass(_agent(nudge_thread="never", reply_on_behalf="auto"))
    crm_write.assert_not_called()
    assert channel.sends == []
    assert [(p.kind, p.outcome) for p in result.proposals] == [("reply_on_behalf", "unsupported")]
    assert [s.tool_output["outcome"] for s in steps if s.tool_name == PROPOSAL_STEP] == [
        "unsupported"
    ]


# ── an Approve All tap must not outlive the pass ──────────────────────


async def test_an_earlier_approve_all_does_not_skip_the_card_on_a_later_pass(enforce, tmp_path):
    from robothor.engine.permission_escalation import PermissionEscalationManager

    manager = PermissionEscalationManager()
    manager._announce = AsyncMock()  # type: ignore[method-assign]
    asked = AsyncMock(return_value=False)
    manager._ask_over_channel = asked  # type: ignore[method-assign]
    # What "Approve All" on an earlier pass's card would have left behind
    # under the old, pass-independent guardrail name.
    manager._session_grants["example-agent:ambient_tier"] = {f"{PROPOSAL_STEP}:nudge_thread"}

    channel = _Channel()
    worker = _worker(
        _Runner("### nudge_thread inbox:12\nAlice is waiting.\n"),
        channel,
        approver=manager,
        digest_dir=tmp_path,
    )
    first = await worker.run_pass(_agent())
    second = await worker.run_pass(_agent())

    assert asked.await_count == 2, "each pass must ask a person"
    assert channel.sends == []
    assert [p.outcome for p in first.proposals + second.proposals] == ["denied", "denied"]
    guardrails = {call.args[0].guardrail_name for call in asked.await_args_list}
    assert len(guardrails) == 2, "a grant must be scoped to one pass"


async def test_approvals_in_one_pass_are_asked_concurrently(enforce, tmp_path):
    class SlowApprover:
        async def request_approval(self, **kwargs: Any) -> bool:
            await asyncio.sleep(0.2)
            return False

    output = "".join(f"### nudge_thread inbox:{i}\nWaiting {i}.\n" for i in range(5))
    worker = _worker(
        _Runner(output),
        _Channel(),
        threads=[_thread(str(i)) for i in range(5)],
        approver=SlowApprover(),
        digest_dir=tmp_path,
    )
    started = time.perf_counter()
    result = await worker.run_pass(_agent())
    elapsed = time.perf_counter() - started
    assert [p.outcome for p in result.proposals] == ["denied"] * 5
    assert elapsed < 0.6, f"five 0.2s approvals took {elapsed:.2f}s: asked one at a time"


# ── what the composing run produced decides nothing unless it finished ─


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED])
async def test_an_unfinished_run_proposes_nothing(enforce, tmp_path, status):
    channel = _Channel()
    worker = _worker(
        _Runner("### send_digest\nLet me check the conversations first", status),
        channel,
        digest_dir=tmp_path,
    )
    result = await worker.run_pass(_agent(send_digest="auto", nudge_thread="never"))
    assert result.proposals == []
    assert channel.sends == []


async def test_output_without_section_headers_is_not_sent_as_a_digest(enforce, tmp_path):
    channel = _Channel()
    worker = _worker(_Runner("Let me check the conversations first."), channel, digest_dir=tmp_path)
    result = await worker.run_pass(_agent(send_digest="auto", nudge_thread="never"))
    assert result.proposals == []
    assert channel.sends == []


# ── the composing run holds read-only tools as the registry builds them ─


@pytest.mark.parametrize("agent_id", ["example-agent", "main"])
@pytest.mark.parametrize("allowed", [[], ["read_file", "create_task", "send_message"]])
def test_the_registry_gives_the_composing_run_only_read_only_tools(agent_id, allowed):
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.engine.tools.registry import get_registry

    agent = _agent(agent_id)
    agent.tools_allowed = list(allowed)
    names = set(get_registry().get_tool_names(composing_config(agent)))
    assert names, "the run still needs some tool list"
    assert names <= READONLY_TOOLS, sorted(names - READONLY_TOOLS)


# ── the inbox read must not cut the longest-waiting threads ───────────


def test_the_inbox_read_pages_past_the_first_hundred_conversations():
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": i} for i in range(100, 130)]}

    def list_conversations(status="open", page=1, page_size=25, tenant_id="", **kw):
        assert status == "open"
        return pages.get(page, [])[:page_size] if page_size >= 100 else []

    message = {"message_type": "incoming", "created_at": NOW, "private": False}
    with (
        patch("robothor.crm.dal.list_conversations", side_effect=list_conversations),
        patch("robothor.crm.dal.list_messages", return_value=[message]),
    ):
        threads = _inbox_threads("test-tenant")
    assert sorted(int(t.thread_id) for t in threads) == list(range(130))


# ── the scheduler runs an ambient pass under the same guards as any run ─


def _scheduler():
    from robothor.engine.scheduler import CronScheduler

    config = MagicMock(default_timezone="UTC", tenant_id="test-tenant", manifest_dir="/nowhere")
    return CronScheduler(config, MagicMock())


def _patches(sched, *, admitted=True, tripped=False):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("robothor.engine.leader.is_leader", return_value=True))
    stack.enter_context(
        patch("robothor.engine.scheduler.load_agent_config_or_broken", return_value=_agent())
    )
    stack.enter_context(
        patch("robothor.engine.scheduler.try_acquire", AsyncMock(return_value=True))
    )
    stack.enter_context(patch("robothor.engine.scheduler.release", AsyncMock()))
    admit = stack.enter_context(patch("robothor.engine.scheduler.admit", return_value=admitted))
    register = stack.enter_context(patch("robothor.engine.scheduler.register"))
    complete = stack.enter_context(patch("robothor.engine.scheduler.complete"))
    state = stack.enter_context(patch("robothor.engine.scheduler.update_schedule_state"))
    stack.enter_context(patch.object(sched, "_circuit_breaker_tripped", return_value=tripped))
    run_pass = stack.enter_context(
        patch(
            "robothor.engine.ambient.AmbientWorker.run_pass",
            AsyncMock(return_value=MagicMock(run_id="r1", run_status="completed", proposals=[])),
        )
    )
    return stack, {
        "admit": admit,
        "register": register,
        "complete": complete,
        "state": state,
        "run_pass": run_pass,
    }


async def test_a_refused_admission_starts_no_composing_run():
    sched = _scheduler()
    stack, m = _patches(sched, admitted=False)
    with stack:
        await sched._run_ambient("example-agent")
    m["run_pass"].assert_not_awaited()
    m["register"].assert_not_called()


async def test_a_tripped_breaker_starts_no_composing_run():
    sched = _scheduler()
    stack, m = _patches(sched, tripped=True)
    with stack:
        await sched._run_ambient("example-agent")
    m["run_pass"].assert_not_awaited()


async def test_an_admitted_pass_is_registered_completed_and_recorded():
    sched = _scheduler()
    stack, m = _patches(sched)
    with stack:
        await sched._run_ambient("example-agent")
    m["run_pass"].assert_awaited_once()
    m["register"].assert_called_once_with("example-agent:ambient", "example-agent")
    m["complete"].assert_called_once_with("example-agent:ambient")
    call = m["state"].call_args
    assert call.kwargs["agent_id"] == "example-agent:ambient"
    assert call.kwargs["last_status"] == "completed"
    assert call.kwargs["last_run_id"] == "r1"
