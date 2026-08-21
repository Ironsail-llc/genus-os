"""Tests for AgentRunner._guard_trailing_assistant — the prefill-rejection guard.

OpenRouter-proxied Anthropic (via Azure/Google providers) rejects requests
whose final message is an assistant turn:
    "This model does not support assistant message prefill.
     The conversation must end with a user message."

We saw this on `main` sub_agent runs at Apr 23 00:14-00:21 calling
claude-sonnet-4.6. The guard drops a trailing assistant message before the
LLM call so the request is accepted.
"""

from __future__ import annotations

from robothor.engine.runner import AgentRunner


class TestGuardTrailingAssistant:
    def test_empty_list_unchanged(self) -> None:
        assert AgentRunner._guard_trailing_assistant([]) == []

    def test_ending_with_user_untouched(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        out = AgentRunner._guard_trailing_assistant(msgs)
        assert out == msgs  # identity — untouched
        assert out is msgs  # no copy when no change

    def test_ending_with_tool_untouched(self) -> None:
        # tool_result turns are valid terminators — LLM can produce assistant next
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        ]
        out = AgentRunner._guard_trailing_assistant(msgs)
        assert out == msgs

    def test_ending_with_assistant_drops_last(self) -> None:
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "orphaned reply"},
        ]
        out = AgentRunner._guard_trailing_assistant(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "user"

    def test_original_list_not_mutated(self) -> None:
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "orphaned reply"},
        ]
        _ = AgentRunner._guard_trailing_assistant(msgs)
        # Original must still have both entries — guard returns a slice, not in-place
        assert len(msgs) == 2
        assert msgs[-1]["role"] == "assistant"

    def test_every_trailing_assistant_dropped_in_one_pass(self) -> None:
        # Two trailing assistants (pathological) are BOTH dropped. The guard
        # runs once per call — the earlier "drop only the last, a later pass
        # catches the rest" behavior left the request still ending on an
        # assistant turn, so the provider rejected it anyway.
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        out = AgentRunner._guard_trailing_assistant(msgs)
        assert len(out) == 1
        assert out[-1]["role"] == "user"
