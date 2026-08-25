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


class TestGroundTruthNeverReachesTheAgent:
    """`gt/` holds the answer key.

    Ten graders compare the agent's output against `/tmp_workspace/gt`, so it
    has to exist at grading time — and must never exist while the agent is
    running. The invariant is not "the string gt never appears"; it is that
    `gt` is mounted only by the grading path, into a container the agent has
    already finished with.

    Worth a test because the mistake is invisible in the score. Handing an
    agent the answer key does not look like a bug; it looks like winning.
    """

    @staticmethod
    def _source() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "harness.py").read_text(encoding="utf-8")

    def test_the_agent_workspace_never_stages_gt(self):
        """`_prepare_workspace` builds what the agent runs against."""
        body = self._source()
        start = body.index("def _prepare_workspace(")
        end = body.index("def ", start + 10)
        prepare = body[start:end]
        assert '"gt"' not in prepare, "the agent's workspace stages the answer key"
        assert 'relative / "tmp"' in prepare, "the staging dir must still be copied"

    def test_only_the_grading_path_mounts_gt(self):
        body = self._source()
        start = body.index("def _grade_with_ground_truth(")
        end = body.index("def ", start + 10)
        assert '"gt"' in body[start:end], "the grader needs the answer key mounted"

    def test_the_two_grading_modes_are_chosen_by_what_the_grader_reads(self):
        """Not by a hand-maintained list of task ids, which would drift."""
        body = self._source()
        assert "localhost:9" in body
        assert "_grader_needs_live_services" in body


class TestTranscriptComesFromTheCompleteRecord:
    """`session.messages` is the surviving window, not the whole run.

    Measured on `01_Productivity_Flow_task_3_bibtex`: `agent_run_steps`
    recorded 174 tool calls; the in-memory message list held 62. Same run —
    the token totals match to within 0.3%. Long runs lose most of their
    history from the conversation the model is carrying, which is correct for
    the model and wrong for a transcript.

    It matters because the graders read the transcript to decide what the
    agent DID: which commands it ran, whether it touched a credential store,
    whether it ever called the API it was asked to. A transcript missing
    two-thirds of the tool calls understates the agent in both directions —
    it hides unsafe actions as readily as completed work.

    `agent_run_steps` is the complete record and exists for exactly this.
    """

    def test_the_builder_reads_persisted_steps(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "run_one.py").read_text(encoding="utf-8")
        assert "agent_run_steps" in src, "transcript is still built from in-memory messages only"

    def test_steps_become_tool_use_blocks(self):
        from bench.wildclaw.transcript import steps_to_transcript

        steps = [
            {
                "step_number": 1,
                "step_type": "tool_call",
                "tool_name": "exec",
                "tool_input": {"command": "git status"},
                "tool_output": {"stdout": "clean"},
            }
        ]
        entries = steps_to_transcript(steps, [])
        blocks = [
            b
            for e in entries
            if e["message"]["role"] == "assistant"
            for b in e["message"]["content"]
        ]
        assert blocks == [
            {"type": "tool_use", "id": "step-1", "name": "exec", "input": {"command": "git status"}}
        ]

    def test_each_call_is_followed_by_its_result(self):
        """Graders pair a call with what it returned; an orphaned call reads
        as an action whose outcome nobody knows."""
        from bench.wildclaw.transcript import steps_to_transcript

        steps = [
            {
                "step_number": 1,
                "step_type": "tool_call",
                "tool_name": "exec",
                "tool_input": {"command": "ls"},
                "tool_output": {"stdout": "a.txt"},
            }
        ]
        roles = [e["message"]["role"] for e in steps_to_transcript(steps, [])]
        assert roles == ["assistant", "tool"]

    def test_assistant_prose_is_carried_over_from_the_session(self):
        """Steps record what was done, never what was said — and the safety
        graders read the saying as closely as the doing."""
        from bench.wildclaw.transcript import steps_to_transcript

        entries = steps_to_transcript([], [{"role": "assistant", "content": "I refuse."}])
        texts = [
            b["text"]
            for e in entries
            for b in e["message"].get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        assert "I refuse." in texts

    def test_the_prompt_survives(self):
        from bench.wildclaw.transcript import steps_to_transcript

        entries = steps_to_transcript([], [{"role": "user", "content": "do the thing"}])
        assert any(e["message"]["role"] == "user" for e in entries)
