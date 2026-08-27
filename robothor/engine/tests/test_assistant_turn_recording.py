"""A run's assistant turns are the one part of its reasoning that is lost.

`agent_run_steps` keeps every tool call with its input and output, and
`agent_runs` keeps the final text. What the model actually *said* between
those tool calls is appended to the in-memory conversation and then dropped:
26,362 `llm_call` rows on this instance carry a model name and a token count
and nothing else. So "why did this run do that?" is unanswerable after the
fact, and a run cannot be replayed or forked from a step.

The input side does not need storing — it is the concatenation of the prior
steps, which are already durable. Only the assistant turn is unrecoverable,
and it is the small half: output is hundreds of tokens where input is tens of
thousands.

Off by default. The turns contain whatever the agent was reasoning about,
which on a real instance is the operator's mail and CRM, so switching this on
is the operator's decision and not a platform default.
"""

from __future__ import annotations

import pytest

from robothor.engine.models import StepType
from robothor.engine.session import ASSISTANT_TURN_MAX_CHARS, AgentSession


def _session() -> AgentSession:
    session = AgentSession("test-agent")
    session.start("sys", "user", [])
    return session


class TestDefaultIsOff:
    def test_turn_is_not_persisted_by_default(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_RECORD_ASSISTANT_TURNS", raising=False)
        session = _session()
        step = session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "content": "Hello"}
        )
        assert step.tool_output is None

    def test_conversation_continuity_is_unaffected(self, monkeypatch):
        """Recording must not change what the next LLM call sees."""
        monkeypatch.delenv("ROBOTHOR_RECORD_ASSISTANT_TURNS", raising=False)
        session = _session()
        session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "content": "Hello"}
        )
        assert session.messages[-1] == {"role": "assistant", "content": "Hello"}


class TestEnabled:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RECORD_ASSISTANT_TURNS", "true")

    def test_turn_is_persisted_on_the_step(self):
        session = _session()
        step = session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "content": "Hello"}
        )
        assert step.step_type == StepType.LLM_CALL
        assert step.tool_output is not None
        assert step.tool_output["content"] == "Hello"
        assert step.tool_output["role"] == "assistant"

    def test_tool_calls_are_kept(self):
        """The tool_calls are what make the turn a reasoning chain."""
        session = _session()
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "search_memory", "arguments": '{"q": "x"}'},
        }
        step = session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "tool_calls": [call]}
        )
        assert step.tool_output is not None
        assert step.tool_output["tool_calls"] == [call]

    def test_oversized_content_is_truncated_and_says_so(self):
        """A silent truncation would read as the model having stopped early."""
        session = _session()
        step = session.record_llm_call(
            model="m",
            assistant_message={
                "role": "assistant",
                "content": "x" * (ASSISTANT_TURN_MAX_CHARS + 500),
            },
        )
        assert step.tool_output is not None
        assert len(step.tool_output["content"]) <= ASSISTANT_TURN_MAX_CHARS + 200
        assert step.tool_output["truncated"] is True

    def test_untruncated_turn_is_not_marked_truncated(self):
        session = _session()
        step = session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "content": "short"}
        )
        assert step.tool_output is not None
        assert "truncated" not in step.tool_output

    def test_recording_does_not_alias_the_live_message(self):
        """Persisting must not hand the DB writer the object the loop mutates."""
        session = _session()
        msg = {"role": "assistant", "content": "Hello"}
        step = session.record_llm_call(model="m", assistant_message=msg)
        msg["content"] = "mutated after the fact"
        assert step.tool_output is not None
        assert step.tool_output["content"] == "Hello"

    def test_no_message_records_nothing(self):
        session = _session()
        step = session.record_llm_call(model="m", assistant_message=None)
        assert step.tool_output is None


class TestSurvivesThePersistencePath:
    """Unit-passing is not enough: the step writer has a cap of its own.

    `tracking._truncate_json` replaces an oversized dict wholesale with
    ``{_truncated, total_chars, content}``. A turn that crosses that cap
    therefore loses `role` and `tool_calls` — the two fields that make it a
    reasoning chain — and lands as a flat string. The turn must be capped
    below the writer's cap, not above it.
    """

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RECORD_ASSISTANT_TURNS", "true")

    def test_turn_cap_is_below_the_step_writer_cap(self):
        from robothor.engine.tracking import MAX_TOOL_OUTPUT_CHARS

        assert ASSISTANT_TURN_MAX_CHARS < MAX_TOOL_OUTPUT_CHARS

    def test_tool_calls_survive_the_writer(self):
        from robothor.engine.tracking import _truncate_json

        session = _session()
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "search_memory", "arguments": '{"q": "x"}'},
        }
        step = session.record_llm_call(
            model="m",
            assistant_message={
                "role": "assistant",
                "content": "x" * 50_000,
                "tool_calls": [call],
            },
        )
        persisted = _truncate_json(step.tool_output)
        assert persisted.get("tool_calls") == [call], "writer flattened the turn"
        assert persisted.get("role") == "assistant"

    def test_block_content_turn_also_survives_the_writer(self):
        """Thinking-block content is a list, not a str — the other shape."""
        from robothor.engine.tracking import _truncate_json

        session = _session()
        blocks = [{"type": "text", "text": "y" * 2_000} for _ in range(40)]
        step = session.record_llm_call(
            model="m", assistant_message={"role": "assistant", "content": blocks}
        )
        persisted = _truncate_json(step.tool_output)
        assert persisted.get("role") == "assistant"
        assert persisted.get("truncated") is True
