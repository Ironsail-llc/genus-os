"""Ambient follow-ups have one code path: the manifest's ``ambient:`` block.

There is no instance-wide switch in front of them. An agent whose manifest
declares the block gets its ambient job and its pass; what goes out is decided
per proposal by the block's tiers. Because this is the part of the engine that
sends messages on its own, a tier the block does not state is ``approve`` (or
stricter), never ``auto``: every unstated proposal goes through the approval
gate.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from robothor.engine import feature_flags
from robothor.engine.ambient import (
    ACTION_KINDS,
    DEFAULT_TIERS,
    PROPOSAL_STEP,
    AmbientWorker,
    Message,
    Thread,
    parse_ambient,
)
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.engine.schedule_reconcile import KIND_AMBIENT, desired_job_specs

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[3]

# A block that states no tiers at all.
UNTIERED_BLOCK = {"watch": ["inbox"], "stalled_after": "4h", "digest_at": "0 9 * * *"}


def _agent() -> AgentConfig:
    return AgentConfig(
        id="example-agent",
        name="Example Agent",
        delivery_channel="telegram",
        delivery_to="12345",
        ambient=parse_ambient(UNTIERED_BLOCK),
    )


class _Runner:
    def __init__(self, output: str) -> None:
        self.output, self.calls = output, []

    async def execute(self, **kwargs: Any) -> AgentRun:
        self.calls.append(kwargs)
        return AgentRun(
            agent_id=kwargs["agent_id"],
            trigger_type=TriggerType.CRON,
            status=RunStatus.COMPLETED,
            output_text=self.output,
        )


class _Channel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []

    async def send(self, target: str, text: str, **kw: Any) -> Any:
        from robothor.engine.channels.base import SendReceipt

        self.sends.append((target, text))
        return SendReceipt(acknowledged=1, expected=1)


class _Approver:
    def __init__(self, answer: bool) -> None:
        self.answer, self.requests = answer, []

    async def request_approval(self, **kwargs: Any) -> bool:
        self.requests.append(kwargs)
        return self.answer


OUTPUT = """\
### nudge_thread inbox:12
Alice has waited six hours on her invoice.

### send_digest
One thread is waiting: Alice, six hours.
"""


def _worker(runner: _Runner, channel: _Channel, approver: _Approver, steps: list[Any], tmp_path):
    async def load(agent: AgentConfig) -> list[Thread]:
        return [
            Thread(
                source="inbox",
                thread_id="12",
                person="Alice",
                messages=[Message("inbound", datetime(2026, 10, 31, 6, 0, tzinfo=UTC))],
            )
        ]

    async def record(new: list[Any]) -> None:
        steps.extend(new)

    return AmbientWorker(
        runner,
        load_threads=load,
        channel_resolver=lambda name: channel,
        approver=lambda: approver,
        record_steps=record,
        digest_dir=tmp_path,
        now=lambda: NOW,
    )


class TestThereIsNoSwitch:
    def test_feature_flags_has_no_ambient_reader(self):
        assert not [name for name in dir(feature_flags) if "ambient" in name.lower()]
        assert "ROBOTHOR_AMBIENT" not in inspect.getsource(feature_flags)

    def test_no_registry_declares_it(self):
        from robothor.settings import model

        assert "ROBOTHOR_AMBIENT" not in inspect.getsource(model)
        flags = yaml.safe_load((REPO / "infra" / "flags.yaml").read_text(encoding="utf-8"))
        names = {entry.get("name") for entry in flags.get("flags", [])}
        assert "ROBOTHOR_AMBIENT" not in names

    def test_an_opted_in_agent_gets_its_job_whatever_the_env_says(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_AMBIENT", "off")
        specs, errors = desired_job_specs([_agent(), AgentConfig(id="plain", name="Plain")])
        assert errors == []
        assert [s.job_id for s in specs.values() if s.kind == KIND_AMBIENT] == [
            "example-agent:ambient"
        ]

    async def test_an_opted_in_agent_runs_its_pass_whatever_the_env_says(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("ROBOTHOR_AMBIENT", "off")
        runner, channel, approver, steps = _Runner(OUTPUT), _Channel(), _Approver(True), []
        result = await _worker(runner, channel, approver, steps, tmp_path).run_pass(_agent())
        assert len(runner.calls) == 1
        assert {s.tool_name for s in steps} == {PROPOSAL_STEP}
        assert sorted(p.outcome for p in result.proposals) == ["sent", "sent"]


class TestAnUnstatedTierIsApprove:
    def test_no_kind_defaults_to_auto(self):
        assert set(DEFAULT_TIERS) == set(ACTION_KINDS)
        assert "auto" not in DEFAULT_TIERS.values()
        assert DEFAULT_TIERS["nudge_thread"] == "approve"
        assert DEFAULT_TIERS["send_digest"] == "approve"
        # Stricter than approve: there is no delivery path for it yet.
        assert DEFAULT_TIERS["reply_on_behalf"] in ("approve", "never")

    def test_a_block_without_tiers_parses_to_the_defaults(self):
        cfg = parse_ambient(UNTIERED_BLOCK)
        assert cfg is not None
        assert cfg.tiers == DEFAULT_TIERS

    def test_an_unrecognised_tier_falls_back_to_the_default_not_auto(self):
        cfg = parse_ambient(UNTIERED_BLOCK | {"tiers": {"nudge_thread": "yes", "send_digest": 1}})
        assert cfg is not None
        assert cfg.tiers["nudge_thread"] == "approve"
        assert cfg.tiers["send_digest"] == "approve"

    def test_the_manifest_schema_writes_the_same_defaults_down(self):
        for path in ("robothor/engine/schema/agent_manifest.yaml", "docs/agents/schema.yaml"):
            schema = yaml.safe_load((REPO / path).read_text(encoding="utf-8"))
            tiers = _find(schema, "ambient")["tiers"]
            assert {k: v["default"] for k, v in tiers.items()} == DEFAULT_TIERS, path

    async def test_every_untiered_proposal_asks_the_gate_and_a_denial_sends_nothing(self, tmp_path):
        runner, channel, approver, steps = _Runner(OUTPUT), _Channel(), _Approver(False), []
        result = await _worker(runner, channel, approver, steps, tmp_path).run_pass(_agent())
        assert sorted(r["tool_name"] for r in approver.requests) == [
            f"{PROPOSAL_STEP}:nudge_thread",
            f"{PROPOSAL_STEP}:send_digest",
        ]
        assert channel.sends == []
        assert [p.outcome for p in result.proposals] == ["denied", "denied"]


def _find(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find(value, key)
            if found is not None:
                return found
    return None
