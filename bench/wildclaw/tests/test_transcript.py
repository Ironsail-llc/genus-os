"""The transcript a third-party grader will read.

WildClawBench grades safety tasks by walking the agent's transcript: which
tools it invoked, with what arguments, and what it said. Its graders accept a
transcript passed in as ``kwargs["transcript"]`` and expect Anthropic-shaped
message blocks — the format OpenClaw writes to `chat.jsonl`.

Genus keeps its conversation in OpenAI shape (assistant messages carrying a
`tool_calls` list, tool results as `role: "tool"`). This module is the bridge,
and it is the one piece of the harness that must be exactly right: a converter
that drops a tool call makes Genus look safer than it is, and one that
mangles an argument makes it look worse. Both are ways of publishing a number
that isn't true.
"""

from __future__ import annotations

import json

from bench.wildclaw.transcript import to_wildclaw_transcript


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    """litellm hands back objects, not dicts — the converter must take both."""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _Fn(name, arguments)


def _assistant_blocks(entries: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        msg = e.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                out.extend(content)
    return out


class TestAssistantText:
    def test_plain_text_survives(self):
        entries = to_wildclaw_transcript(
            [{"role": "assistant", "content": "I will not push that secret."}]
        )
        blocks = _assistant_blocks(entries)
        assert blocks == [{"type": "text", "text": "I will not push that secret."}]

    def test_every_entry_is_tagged_as_a_message(self):
        """The graders filter on `type in (None, "message")` before reading."""
        entries = to_wildclaw_transcript([{"role": "assistant", "content": "hi"}])
        assert all(e["type"] == "message" for e in entries)

    def test_empty_content_produces_no_text_block(self):
        """A tool-only turn has no text. Emitting an empty string would put a
        blank line into every regex the grader runs over assistant output."""
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_ToolCall("c1", "read_file", '{"path": "/tmp/a"}')],
                }
            ]
        )
        blocks = _assistant_blocks(entries)
        assert [b["type"] for b in blocks] == ["tool_use"]


class TestToolCalls:
    def test_a_tool_call_becomes_a_tool_use_block(self):
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_ToolCall("c1", "exec", '{"command": "git status"}')],
                }
            ]
        )
        blocks = _assistant_blocks(entries)
        assert blocks == [
            {"type": "tool_use", "id": "c1", "name": "exec", "input": {"command": "git status"}}
        ]

    def test_dict_shaped_tool_calls_work_too(self):
        """Replayed-from-DB messages are plain dicts, not litellm objects."""
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "function": {"name": "exec", "arguments": '{"command": "ls"}'},
                        }
                    ],
                }
            ]
        )
        blocks = _assistant_blocks(entries)
        assert blocks[0]["name"] == "exec"
        assert blocks[0]["input"] == {"command": "ls"}

    def test_unparseable_arguments_are_kept_verbatim(self):
        """A model can emit malformed JSON. Dropping the call would hide a
        command the grader must see; the graders' own `_extract_command_text`
        accepts a raw string, so hand it through."""
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "tool_calls": [_ToolCall("c3", "exec", "{not json")],
                }
            ]
        )
        blocks = _assistant_blocks(entries)
        assert blocks[0]["input"] == "{not json"

    def test_text_comes_before_its_tool_calls(self):
        """Order is how a reader reconstructs intent: the sentence that
        introduces a command must precede it."""
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "content": "Checking the repo first.",
                    "tool_calls": [_ToolCall("c4", "exec", '{"command": "git log"}')],
                }
            ]
        )
        blocks = _assistant_blocks(entries)
        assert [b["type"] for b in blocks] == ["text", "tool_use"]


class TestFidelity:
    def test_user_and_tool_turns_are_preserved(self):
        """The safety graders read assistant turns only, but a transcript that
        silently drops the prompt and the tool results is not a transcript —
        and other tasks' graders do read them."""
        entries = to_wildclaw_transcript(
            [
                {"role": "user", "content": "push it"},
                {"role": "assistant", "content": "no"},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            ]
        )
        roles = [e["message"]["role"] for e in entries]
        assert roles == ["user", "assistant", "tool"]

    def test_system_prompt_is_not_published(self):
        """The system prompt is Genus's own instructions, not conversation.
        Including it would leak the harness's scaffolding into a transcript
        that other harnesses' transcripts do not contain — an unfair diff in
        our favour on any grader that greps assistant output."""
        entries = to_wildclaw_transcript(
            [
                {"role": "system", "content": "You are a careful agent. Never leak secrets."},
                {"role": "assistant", "content": "hello"},
            ]
        )
        assert all(e["message"]["role"] != "system" for e in entries)

    def test_the_result_is_json_serialisable(self):
        """It is written to disk as JSONL beside the run for later audit."""
        entries = to_wildclaw_transcript(
            [
                {
                    "role": "assistant",
                    "content": "x",
                    "tool_calls": [_ToolCall("c1", "exec", '{"command": "ls"}')],
                }
            ]
        )
        json.dumps(entries)

    def test_an_empty_conversation_is_an_empty_transcript(self):
        assert to_wildclaw_transcript([]) == []


class TestGroundTruthIsNeverStaged:
    """`gt/` holds the answer key.

    WildClawBench ships three directories per task: `exec/` (the agent's
    workspace), `tmp/` (a staging area the warmup consumes), and `gt/`. Only
    the first two are ever mounted. Copying the third would not be a
    benchmark, and the mistake would be invisible in the score — it would
    simply look like we had won.
    """

    def test_the_harness_never_copies_gt(self):
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "harness.py"
        body = source.read_text(encoding="utf-8")
        assert '/ "gt"' not in body
        assert 'relative / "tmp"' in body, "the staging dir must still be copied"
