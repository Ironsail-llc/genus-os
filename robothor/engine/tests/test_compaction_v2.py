"""Compaction v2 (``ROBOTHOR_COMPACTION_V2``): the hardened compaction path.

Five behaviours, each one a way the v1 path loses a long run's work:

* it summarised with one hard-coded external model whenever a caller passed no
  chain, so a deployment whose fleet runs on other routes dialled a model it may
  not be authorised for. v2 uses the agent's own chain, the fleet chain when
  there is none, and a chain member the manifest names for compaction first;
* the cheapest tokens to reclaim (tool-call argument JSON, historical media,
  duplicated and oversized old tool results) were left for the LLM passes. v2
  reclaims them first, with no model call at all;
* the summary was rebuilt from scratch, segment by segment, every time. v2
  feeds the previous summary plus the new slice to one call;
* a context that grew by one tool result compacted again on the next
  iteration. v2 waits ``ROBOTHOR_COMPACTION_MIN_GAP_ITERATIONS`` iterations;
* a failed summary became a placeholder and the drain then dropped the middle of
  the conversation. v2 keeps the messages and records the compaction degraded.

The completion every test fakes is ``compaction.pooled_acompletion`` — the name
the module imports, so the real dispatch is never reached.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.compaction import CompactionResult, compact
from robothor.engine.context import estimate_tokens, maybe_compress
from robothor.engine.context_budget import keep_context_within_budget
from robothor.engine.context_control import ContextControl, control

HARD_CODED_MODEL = "gemini/gemini-2.5-flash"
CHAIN = ["openai/example-primary", "vertex_ai/example-small"]


@pytest.fixture(autouse=True)
def _v2_on(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_COMPACTION_V2", "1")
    monkeypatch.delenv("ROBOTHOR_RIP_18_ENABLED", raising=False)
    monkeypatch.setenv("ROBOTHOR_COMPACTION_MIN_GAP_ITERATIONS", "3")
    reset_settings()
    yield
    reset_settings()


class FakeCompletion:
    """Records every call; answers JSON for extraction and text for a summary."""

    def __init__(self, *, fail: bool = False, summary: str = "NEW-SUMMARY-TEXT"):
        self.fail = fail
        self.summary = summary
        self.calls: list[dict] = []

    async def __call__(self, model: str, **kwargs):
        self.calls.append({"model": model, **kwargs})
        if self.fail:
            raise RuntimeError(f"{model} is unreachable")
        if kwargs.get("response_format"):
            content = json.dumps({"facts": [{"category": "decision", "text": "kept", "priority": 4}]})
        else:
            content = self.summary
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )

    @property
    def summary_calls(self) -> list[dict]:
        return [c for c in self.calls if not c.get("response_format")]


def _head() -> list[dict]:
    return [
        {"role": "system", "content": "You are example-agent."},
        {"role": "user", "content": "Write the quarterly report for Alice."},
        {"role": "assistant", "content": "Starting."},
    ]


def _tool_heavy_fixture(rounds: int = 30) -> list[dict]:
    """A long agentic run whose bulk is argument JSON, media and repeated output."""
    msgs = _head()
    listing = json.dumps({"files": [f"row-{i}.csv" for i in range(300)]})  # ~3.6K chars
    for i in range(rounds):
        call_id = f"call-{i}"
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": f"out/part-{i}.md", "content": f"section {i} " * 700}
                            ),
                        },
                    }
                ],
            }
        )
        # Every third result is the same directory listing: the dedupe's food.
        result = listing if i % 3 == 0 else f"wrote out/part-{i}.md"
        msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        if i % 5 == 0:
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"screenshot {i}"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + "A" * 20_000},
                        },
                    ],
                }
            )
            msgs.append({"role": "assistant", "content": f"Seen screenshot {i}."})
    return msgs


def _prose_fixture(turns: int = 60) -> list[dict]:
    msgs = _head()
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"old-turn-{i} " + "detail " * 300})
    return msgs


# ── AC1: the LLM-free pre-pass ────────────────────────────────────────


async def test_pre_pass_halves_the_fixture_with_zero_llm_calls():
    messages = _tool_heavy_fixture()
    before = estimate_tokens(messages)
    fake = FakeCompletion()

    with patch("robothor.engine.compaction.pooled_acompletion", new=fake):
        result = await compact(messages, models=CHAIN, threshold=1, drain_to=before // 2)

    after = estimate_tokens(result.messages)
    print(f"\npre-pass fixture: {before} -> {after} tokens ({1 - after / before:.1%} reclaimed)")
    assert fake.calls == []
    assert after <= before * 0.5
    # The protected head is never touched by the pre-pass.
    assert result.messages[:3] == messages[:3]


async def test_pre_pass_keeps_argument_json_parseable():
    """A provider replaying the history parses every call's arguments."""
    messages = _tool_heavy_fixture()
    with patch("robothor.engine.compaction.pooled_acompletion", new=FakeCompletion()):
        result = await compact(messages, models=CHAIN, threshold=1, drain_to=10**9 // 2)
    for msg in result.messages:
        for call in msg.get("tool_calls") or []:
            json.loads(call["function"]["arguments"])


# ── AC2: freeze on failure ────────────────────────────────────────────


async def test_a_failed_summary_keeps_every_message():
    messages = _prose_fixture()
    fake = FakeCompletion(fail=True)

    with patch("robothor.engine.compaction.pooled_acompletion", new=fake):
        result = await compact(messages, models=CHAIN, threshold=1, drain_to=1000)

    assert fake.calls, "the summariser was never asked"
    assert result.messages == messages
    assert getattr(result, "degraded", False) is True


async def test_a_failed_summary_is_not_drained_and_is_recorded_degraded():
    messages = _prose_fixture()
    state = ContextControl()
    token = control.set(state)
    try:
        with patch(
            "robothor.engine.compaction.pooled_acompletion", new=FakeCompletion(fail=True)
        ):
            out = await maybe_compress(messages, CHAIN, threshold=1000)
    finally:
        control.reset(token)

    assert out == messages
    assert state.measurements and state.measurements[-1].get("degraded") is True


# ── AC3: anti-thrash backoff ──────────────────────────────────────────


def _big_conversation(tag: int) -> list[dict]:
    msgs = _head()
    msgs.extend(
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"iter-{tag} " + "x" * 8_000}
        for i in range(60)
    )
    return msgs


async def _iterate(iterations: list[int]) -> list[int]:
    """Run the in-loop budget at each iteration over a context above threshold."""
    compacted_at: list[int] = []
    current = {"iteration": 0}

    async def fake_compact(messages, **_kwargs):
        compacted_at.append(current["iteration"])
        return CompactionResult(messages=messages[:3], passes_used=3)

    session = SimpleNamespace(
        messages=[], run_id="run-1", thin_previous_tool_results=lambda **_: 0
    )
    config = SimpleNamespace(id="example-agent", eager_tool_compression=False)
    state = ContextControl()
    token = control.set(state)
    try:
        with patch("robothor.engine.compaction.compact", new=fake_compact):
            for it in iterations:
                current["iteration"] = it
                session.messages = _big_conversation(it)
                await keep_context_within_budget(
                    session,
                    config,
                    iteration=it,
                    models=CHAIN,
                    broken_models=set(),
                    hook_registry=None,
                    pre_iteration_msg_idx=0,
                )
    finally:
        control.reset(token)
    return compacted_at


async def test_no_second_compaction_within_the_gap():
    assert await _iterate([1, 2, 3, 4]) == [1, 4]


async def test_without_v2_every_iteration_may_compact(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_COMPACTION_V2", raising=False)
    assert await _iterate([1, 2, 3, 4]) == [1, 2, 3, 4]


# ── AC4: the model comes from the agent's chain ───────────────────────


async def test_every_call_goes_to_a_member_of_the_agent_chain():
    fake = FakeCompletion()
    with patch("robothor.engine.compaction.pooled_acompletion", new=fake):
        await compact(_prose_fixture(), models=CHAIN, threshold=1, drain_to=1000)

    assert fake.calls
    assert {c["model"] for c in fake.calls} <= set(CHAIN)
    assert fake.calls[0]["model"] == CHAIN[0]


async def test_the_manifest_compaction_model_is_tried_first():
    fake = FakeCompletion()
    with patch("robothor.engine.compaction.pooled_acompletion", new=fake):
        await compact(
            _prose_fixture(),
            models=CHAIN,
            threshold=1,
            drain_to=1000,
            preferred_model="vertex_ai/example-small",
        )

    assert fake.calls
    assert all(c["model"] == "vertex_ai/example-small" for c in fake.calls)


async def test_no_chain_means_the_fleet_chain_never_the_hard_coded_model():
    fake = FakeCompletion()
    fleet = ["openai/example-fleet"]
    with (
        patch("robothor.engine.compaction.pooled_acompletion", new=fake),
        patch("robothor.engine.config.fleet_model_chain", return_value=fleet),
    ):
        await compact(_prose_fixture(), models=None, threshold=1, drain_to=1000)

    assert fake.calls
    assert HARD_CODED_MODEL not in {c["model"] for c in fake.calls}
    assert {c["model"] for c in fake.calls} == set(fleet)


async def test_context_budget_passes_the_manifest_compaction_model():
    seen: dict = {}

    async def fake_compact(messages, **kwargs):
        seen.update(kwargs)
        return CompactionResult(messages=messages[:3], passes_used=3)

    session = SimpleNamespace(
        messages=_big_conversation(1), run_id="run-1", thin_previous_tool_results=lambda **_: 0
    )
    config = SimpleNamespace(
        id="example-agent", eager_tool_compression=False, model_compaction=CHAIN[1]
    )
    with patch("robothor.engine.compaction.compact", new=AsyncMock(side_effect=fake_compact)):
        await keep_context_within_budget(
            session,
            config,
            iteration=1,
            models=CHAIN,
            broken_models=set(),
            hook_registry=None,
            pre_iteration_msg_idx=0,
        )
    assert seen.get("preferred_model") == CHAIN[1]


def test_the_manifest_model_block_accepts_a_compaction_model():
    from robothor.engine.config import manifest_to_agent_config

    config = manifest_to_agent_config(
        {
            "id": "example-agent",
            "model": {"primary": CHAIN[0], "fallbacks": [CHAIN[1]], "compaction": CHAIN[1]},
        }
    )
    assert getattr(config, "model_compaction", "") == CHAIN[1]


# ── AC5: the summary is updated, not rebuilt ──────────────────────────


async def test_the_previous_summary_is_updated_in_one_call():
    messages = _head()
    messages.append({"role": "user", "content": "[Conversation summary]\nPREVIOUS-SUMMARY-ALPHA"})
    messages.append({"role": "assistant", "content": "Understood."})
    messages.extend(_prose_fixture()[3:])
    fake = FakeCompletion(summary="UPDATED-SUMMARY-BETA")

    with patch("robothor.engine.compaction.pooled_acompletion", new=fake):
        result = await compact(messages, models=CHAIN, threshold=1, drain_to=1000)

    assert len(fake.summary_calls) == 1
    prompt = "\n".join(str(m.get("content")) for m in fake.summary_calls[0]["messages"])
    assert "PREVIOUS-SUMMARY-ALPHA" in prompt
    summaries = [
        m
        for m in result.messages
        if isinstance(m.get("content"), str) and m["content"].startswith("[Conversation summary]")
    ]
    assert len(summaries) == 1
    assert "UPDATED-SUMMARY-BETA" in summaries[0]["content"]
    assert "PREVIOUS-SUMMARY-ALPHA" not in summaries[0]["content"]
