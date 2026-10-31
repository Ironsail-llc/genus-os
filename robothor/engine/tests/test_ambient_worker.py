"""The ambient pass: compose, record, and send only what the tier allows.

``observe`` is the first rung and the one that matters most: it must record
what it would have sent and send nothing at all — no channel send, no approval
card, no reply in the thread. ``enforce`` sends per tier: ``auto`` sends,
``approve`` goes through the same approval gate a guarded tool call uses, and
``never`` is not even proposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from robothor.engine.ambient import (
    PROPOSAL_STEP,
    AmbientConfig,
    AmbientWorker,
    Message,
    Thread,
)
from robothor.engine.channels.base import SendReceipt
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, StepType, TriggerType

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _agent(**tiers: str) -> AgentConfig:
    return AgentConfig(
        id="example-agent",
        name="Example Agent",
        delivery_channel="telegram",
        delivery_to="12345",
        ambient=AmbientConfig(
            watch=["inbox"],
            stalled_after="4h",
            stalled_after_seconds=4 * 3600,
            digest_at="0 9 * * *",
            tiers={
                "nudge_thread": "approve",
                "send_digest": "auto",
                "reply_on_behalf": "never",
            }
            | tiers,
        ),
    )


def _stalled_thread() -> Thread:
    return Thread(
        source="inbox",
        thread_id="12",
        person="Alice",
        subject="Invoice question",
        messages=[Message(direction="inbound", at=datetime(2026, 10, 31, 6, 0, tzinfo=UTC))],
    )


MODEL_OUTPUT = """\
### nudge_thread inbox:12
Alice asked about her invoice six hours ago and is still waiting.

### reply_on_behalf inbox:12
Hi Alice, thanks for your patience, we are looking into it.

### send_digest
One thread is waiting: Alice (invoice question), six hours.
"""


@dataclass
class FakeRunner:
    output: str = MODEL_OUTPUT
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def execute(self, **kwargs: Any) -> AgentRun:
        self.calls.append(kwargs)
        return AgentRun(
            agent_id=kwargs["agent_id"],
            trigger_type=kwargs["trigger_type"],
            trigger_detail=kwargs.get("trigger_detail"),
            status=RunStatus.COMPLETED,
            output_text=self.output,
        )


@dataclass
class FakeChannel:
    sends: list[tuple[str, str]] = field(default_factory=list)

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append((target, text))
        return SendReceipt(acknowledged=1, expected=1, target=target, body=text)


@dataclass
class FakeApprover:
    answer: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def request_approval(self, **kwargs: Any) -> bool:
        self.requests.append(kwargs)
        return self.answer


@dataclass
class Harness:
    runner: FakeRunner
    channel: FakeChannel
    approver: FakeApprover | None
    steps: list[Any]
    replies: list[tuple[str, str]]
    worker: AmbientWorker


def _harness(
    tmp_path: Path,
    *,
    approver: FakeApprover | None = None,
    output: str = MODEL_OUTPUT,
    threads: list[Thread] | None = None,
) -> Harness:
    runner = FakeRunner(output=output)
    channel = FakeChannel()
    steps: list[Any] = []
    replies: list[tuple[str, str]] = []

    async def load_threads(agent: AgentConfig) -> list[Thread]:
        return [_stalled_thread()] if threads is None else threads

    async def record_steps(new_steps: list[Any]) -> None:
        steps.extend(new_steps)

    async def send_reply(thread: Any, text: str) -> bool:
        replies.append((thread.ref, text))
        return True

    worker = AmbientWorker(
        runner,
        load_threads=load_threads,
        channel_resolver=lambda name: channel,
        approver=lambda: approver,
        record_steps=record_steps,
        send_reply=send_reply,
        digest_dir=tmp_path,
        now=lambda: NOW,
    )
    return Harness(runner, channel, approver, steps, replies, worker)


@pytest.fixture
def observe(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "observe")


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "enforce")


class TestOff:
    async def test_flag_unset_runs_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_AMBIENT", raising=False)
        h = _harness(tmp_path, approver=FakeApprover(answer=True))
        result = await h.worker.run_pass(_agent())
        assert result.mode == "off"
        assert h.runner.calls == []
        assert h.channel.sends == [] and h.steps == []

    async def test_an_agent_without_the_block_runs_nothing(self, tmp_path, observe):
        h = _harness(tmp_path)
        await h.worker.run_pass(AgentConfig(id="plain", name="Plain"))
        assert h.runner.calls == []

    async def test_nothing_stalled_means_no_run(self, tmp_path, observe):
        h = _harness(tmp_path, threads=[])
        result = await h.worker.run_pass(_agent())
        assert h.runner.calls == [] and result.proposals == []


class TestObserveRecordsAndSendsNothing:
    async def test_observe_records_steps_and_a_digest_and_sends_nothing(self, tmp_path, observe):
        approver = FakeApprover(answer=True)
        h = _harness(tmp_path, approver=approver)
        result = await h.worker.run_pass(_agent(reply_on_behalf="auto"))

        assert h.channel.sends == []
        assert approver.requests == []
        assert h.replies == []

        proposal_steps = [s for s in h.steps if s.tool_name == PROPOSAL_STEP]
        assert len(proposal_steps) == 3
        assert {s.step_type for s in proposal_steps} == {StepType.CHECKPOINT}
        assert {s.tool_input["kind"] for s in proposal_steps} == {
            "nudge_thread",
            "reply_on_behalf",
            "send_digest",
        }
        assert {s.tool_output["outcome"] for s in proposal_steps} == {"observed"}
        assert all(s.run_id == result.run_id for s in proposal_steps)

        digest = (tmp_path / "example-agent").glob("*.md")
        text = "\n".join(p.read_text() for p in digest)
        assert "Alice asked about her invoice" in text
        assert "observe" in text

    async def test_the_composing_run_is_the_agents_own_run_and_cannot_send(self, tmp_path, observe):
        h = _harness(tmp_path)
        await h.worker.run_pass(_agent())
        [call] = h.runner.calls
        assert call["agent_id"] == "example-agent"
        assert call["trigger_type"] == TriggerType.CRON
        assert call["trigger_detail"].startswith("ambient:")
        composing = call["agent_config"]
        # The composing run delivers nothing itself and holds only read-only
        # tools: the worker, not the run, decides what is sent.
        assert composing.delivery_mode.value == "none"
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert composing.tools_allowed
        assert set(composing.tools_allowed) <= READONLY_TOOLS
        assert "Alice" in call["message"]


class TestEnforceRoutesThroughTheTier:
    async def test_approve_tier_asks_the_gate_once_and_a_denial_sends_nothing(
        self, tmp_path, enforce
    ):
        approver = FakeApprover(answer=False)
        h = _harness(tmp_path, approver=approver, output=MODEL_OUTPUT.split("### send_digest")[0])
        result = await h.worker.run_pass(_agent())

        assert len(approver.requests) == 1
        req = approver.requests[0]
        assert req["agent_id"] == "example-agent"
        assert req["tool_name"] == f"{PROPOSAL_STEP}:nudge_thread"
        assert "Alice asked about her invoice" in str(req["tool_args"])
        assert h.channel.sends == []
        assert [p.outcome for p in result.proposals if p.kind == "nudge_thread"] == ["denied"]

    async def test_approve_tier_sends_once_when_approved(self, tmp_path, enforce):
        approver = FakeApprover(answer=True)
        h = _harness(tmp_path, approver=approver, output=MODEL_OUTPUT.split("### send_digest")[0])
        await h.worker.run_pass(_agent())
        assert len(approver.requests) == 1
        assert len(h.channel.sends) == 1
        target, text = h.channel.sends[0]
        assert target == "12345"
        assert "Alice asked about her invoice" in text

    async def test_auto_tier_sends_without_asking(self, tmp_path, enforce):
        approver = FakeApprover(answer=False)
        h = _harness(tmp_path, approver=approver)
        result = await h.worker.run_pass(_agent(nudge_thread="never"))
        assert approver.requests == []
        assert [t for _, t in h.channel.sends] == [
            "One thread is waiting: Alice (invoice question), six hours."
        ]
        assert [(p.kind, p.outcome) for p in result.proposals] == [("send_digest", "sent")]

    async def test_approve_tier_with_no_approver_is_not_sent(self, tmp_path, enforce):
        h = _harness(tmp_path, approver=None, output=MODEL_OUTPUT.split("### reply")[0])
        result = await h.worker.run_pass(_agent())
        assert h.channel.sends == []
        assert [p.outcome for p in result.proposals] == ["no_approver"]

    async def test_reply_on_behalf_goes_to_the_thread_not_the_operator(self, tmp_path, enforce):
        h = _harness(tmp_path, approver=FakeApprover(answer=True))
        await h.worker.run_pass(
            _agent(nudge_thread="never", send_digest="never", reply_on_behalf="auto")
        )
        assert h.replies == [
            ("inbox:12", "Hi Alice, thanks for your patience, we are looking into it.")
        ]
        assert h.channel.sends == []

    async def test_every_outcome_is_recorded_as_a_step(self, tmp_path, enforce):
        h = _harness(tmp_path, approver=FakeApprover(answer=False))
        await h.worker.run_pass(_agent())
        outcomes = sorted(
            (s.tool_input["kind"], s.tool_output["outcome"])
            for s in h.steps
            if s.tool_name == PROPOSAL_STEP
        )
        assert outcomes == [("nudge_thread", "denied"), ("send_digest", "sent")]


class TestNeverIsNotEvenProposed:
    async def test_never_kinds_are_not_requested_and_not_recorded(self, tmp_path, observe):
        h = _harness(tmp_path)
        result = await h.worker.run_pass(_agent(reply_on_behalf="never"))
        prompt = h.runner.calls[0]["message"]
        assert "reply_on_behalf" not in prompt
        # The model wrote one anyway; it is dropped, not recorded.
        assert "reply_on_behalf" not in {p.kind for p in result.proposals}
        assert "reply_on_behalf" not in {
            s.tool_input["kind"] for s in h.steps if s.tool_name == PROPOSAL_STEP
        }

    async def test_every_kind_never_means_no_run_at_all(self, tmp_path, observe):
        h = _harness(tmp_path)
        await h.worker.run_pass(
            _agent(nudge_thread="never", send_digest="never", reply_on_behalf="never")
        )
        assert h.runner.calls == []


class TestUnstructuredOutput:
    async def test_plain_text_becomes_the_digest_when_digests_are_allowed(self, tmp_path, observe):
        h = _harness(tmp_path, output="Alice has waited six hours on the invoice thread.")
        result = await h.worker.run_pass(_agent())
        assert [(p.kind, p.text) for p in result.proposals] == [
            ("send_digest", "Alice has waited six hours on the invoice thread.")
        ]

    async def test_a_nudge_for_a_thread_that_is_not_stalled_is_dropped(self, tmp_path, observe):
        h = _harness(tmp_path, output="### nudge_thread inbox:999\nMade up.\n")
        result = await h.worker.run_pass(_agent())
        assert result.proposals == []
