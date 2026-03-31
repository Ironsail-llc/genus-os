"""Tests for eager tool result compression and context offloading."""

from __future__ import annotations

import json
from pathlib import Path

from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession


def _make_session(tool_offload_threshold: int = 0) -> AgentSession:
    """Create a session with some conversation history."""
    s = AgentSession(
        agent_id="test",
        trigger_type=TriggerType.MANUAL,
        tool_offload_threshold=tool_offload_threshold,
    )
    s.start(
        system_prompt="You are a test agent.",
        user_message="Do something.",
        tools_provided=["read_file"],
    )
    return s


class TestThinPreviousToolResults:
    """Tests for AgentSession.thin_previous_tool_results()."""

    def test_large_tool_result_gets_thinned(self):
        s = _make_session()
        # Add an assistant message with tool calls + a large tool result
        s.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc1", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        large_result = json.dumps({"data": "x" * 1000})
        s.messages.append({"role": "tool", "tool_call_id": "tc1", "content": large_result})

        # Current iteration starts after these messages
        protect_idx = len(s.messages)

        # Add another iteration (should be protected)
        s.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc2", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        s.messages.append(
            {"role": "tool", "tool_call_id": "tc2", "content": json.dumps({"big": "y" * 2000})}
        )

        chars_saved = s.thin_previous_tool_results(protect_after_index=protect_idx)

        # First tool result should be thinned
        assert len(s.messages[3]["content"]) < len(large_result)
        assert chars_saved > 0

        # Second tool result (protected) should be untouched
        assert len(s.messages[5]["content"]) > 500

    def test_small_tool_results_untouched(self):
        s = _make_session()
        small_content = json.dumps({"ok": True})
        s.messages.append({"role": "tool", "tool_call_id": "tc1", "content": small_content})

        chars_saved = s.thin_previous_tool_results(protect_after_index=len(s.messages))
        assert chars_saved == 0
        assert s.messages[-1]["content"] == small_content

    def test_non_tool_messages_untouched(self):
        s = _make_session()
        user_msg = "x" * 1000
        s.messages.append({"role": "user", "content": user_msg})

        chars_saved = s.thin_previous_tool_results(protect_after_index=len(s.messages))
        assert chars_saved == 0
        assert s.messages[-1]["content"] == user_msg

    def test_idempotent(self):
        s = _make_session()
        s.messages.append(
            {"role": "tool", "tool_call_id": "tc1", "content": json.dumps({"k": "v" * 600})}
        )
        protect_idx = len(s.messages)

        saved1 = s.thin_previous_tool_results(protect_after_index=protect_idx)
        content_after_first = s.messages[-1]["content"]
        saved2 = s.thin_previous_tool_results(protect_after_index=protect_idx)

        assert saved1 > 0
        assert saved2 == 0  # Already thinned, nothing more to save
        assert s.messages[-1]["content"] == content_after_first

    def test_returns_correct_chars_saved(self):
        s = _make_session()
        original = json.dumps({"data": "z" * 800})
        s.messages.append({"role": "tool", "tool_call_id": "tc1", "content": original})
        protect_idx = len(s.messages)

        chars_saved = s.thin_previous_tool_results(protect_after_index=protect_idx)
        thinned = s.messages[-1]["content"]
        assert chars_saved == len(original) - len(thinned)

    def test_protect_after_index_boundary(self):
        s = _make_session()
        large = json.dumps({"a": "b" * 800})
        # Add two large tool results
        s.messages.append({"role": "tool", "tool_call_id": "tc1", "content": large})
        boundary = len(s.messages)
        s.messages.append({"role": "tool", "tool_call_id": "tc2", "content": large})

        s.thin_previous_tool_results(protect_after_index=boundary)

        # First is thinned (before boundary)
        assert len(s.messages[boundary - 1]["content"]) < len(large)
        # Second is untouched (at/after boundary)
        assert s.messages[boundary]["content"] == large


class TestOffloadToolResult:
    """Tests for context offloading of large tool results."""

    def test_large_result_offloaded_to_file(self):
        s = _make_session(tool_offload_threshold=100)
        large_output = {"data": "x" * 200}

        s.record_tool_call(
            tool_name="read_file",
            tool_input={"path": "/tmp/test"},
            tool_output=large_output,
            tool_call_id="tc1",
        )

        content = s.messages[-1]["content"]
        assert "[Full output:" in content
        assert "read_file to retrieve" in content

        # Extract file path and verify it exists with correct content
        path_start = content.index("[Full output: ") + len("[Full output: ")
        path_end = content.index(" — use read_file")
        offload_path = Path(content[path_start:path_end])
        assert offload_path.exists()
        stored = offload_path.read_text()
        assert json.loads(stored) == large_output
        offload_path.unlink()

    def test_small_result_stays_inline(self):
        s = _make_session(tool_offload_threshold=100)
        small_output = {"ok": True}

        s.record_tool_call(
            tool_name="read_file",
            tool_input={},
            tool_output=small_output,
            tool_call_id="tc1",
        )

        content = s.messages[-1]["content"]
        assert "[Full output:" not in content
        assert json.loads(content) == small_output

    def test_offloading_disabled_when_threshold_zero(self):
        s = _make_session(tool_offload_threshold=0)
        large_output = {"data": "x" * 10000}

        s.record_tool_call(
            tool_name="read_file",
            tool_input={},
            tool_output=large_output,
            tool_call_id="tc1",
        )

        content = s.messages[-1]["content"]
        assert "[Full output:" not in content
        assert json.loads(content) == large_output

    def test_offloaded_file_has_tool_name_prefix(self):
        s = _make_session(tool_offload_threshold=100)

        s.record_tool_call(
            tool_name="web_fetch",
            tool_input={"url": "https://example.com"},
            tool_output={"html": "<div>" + "x" * 500 + "</div>"},
            tool_call_id="tc1",
        )

        content = s.messages[-1]["content"]
        # Should still have untrusted_content wrapper since web_fetch is external
        assert "untrusted_content" in content
        assert "[Full output:" in content

        # Extract path and check prefix
        # Content is wrapped, so parse inside the wrapper
        path_start = content.index("[Full output: ") + len("[Full output: ")
        path_end = content.index(" — use read_file")
        offload_path = Path(content[path_start:path_end])
        assert "tool_web_fetch_" in offload_path.name
        if offload_path.exists():
            offload_path.unlink()
