"""Compaction must not throw away everything the agent learned.

`extract_facts` built its input from `role in ("user", "assistant")` and
`isinstance(content, str)`. **Tool results were excluded by construction.**

In an agentic run, the facts live in tool output. Traced through one
benchmark run: every load-bearing fact — the grid was 15x15, the pitch 58px,
the row/column origins, seven exact clue colours, 100 clue cells — arrived
as `exec` stdout. Post-compaction the retained block was thirteen lines, ten
of them restating one complaint, and the agent's very next act was to list
the input directory and begin extraction again from zero, at the halfway
point of its budget.

The summariser kept the agent's complaints and dropped its findings.

Two changes. Tool results now feed fact extraction, condensed through
`extract_tool_summary` (which already lived in this module and was used only
for in-place shrinking). And a deterministic tail — the paths this run wrote
and the last tool calls it made — survives every compaction without
depending on an LLM call succeeding, because state that must survive should
not rest on `gemini-2.5-flash` answering inside 1000 tokens.
"""

from __future__ import annotations

from robothor.engine.compaction import (
    build_extraction_input,
    deterministic_tail,
)


class TestToolResultsReachFactExtraction:
    def test_a_tool_result_is_included(self):
        messages = [
            {"role": "user", "content": "measure the grid"},
            {"role": "tool", "tool_call_id": "c1", "content": "grid=15x15 pitch=58px"},
        ]
        text = build_extraction_input(messages)
        assert "grid=15x15" in text, "tool findings are still dropped"

    def test_the_role_is_labelled_so_the_summariser_knows_the_source(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "answer=42"}]
        assert "tool:" in build_extraction_input(messages)

    def test_prose_still_included(self):
        messages = [
            {"role": "assistant", "content": "I will now measure the grid"},
            {"role": "user", "content": "go ahead"},
        ]
        text = build_extraction_input(messages)
        assert "measure the grid" in text
        assert "go ahead" in text

    def test_a_long_tool_result_is_condensed_not_dropped(self):
        """The 300-char truncation that applied to prose would shred a table."""
        payload = "row " + ("x" * 40_000)
        messages = [{"role": "tool", "tool_call_id": "c1", "content": payload}]
        text = build_extraction_input(messages)
        assert text, "a large tool result vanished entirely"
        assert len(text) < 20_000, "the whole payload was passed through"

    def test_image_content_blocks_do_not_crash_it(self):
        """A tool result can be a content-block list since view_image shipped."""
        messages = [
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "text", "text": "Image (40x30)"},
                ],
            }
        ]
        text = build_extraction_input(messages)
        assert "AAAA" not in text, "base64 was fed to the summariser"
        assert "Image (40x30)" in text

    def test_an_empty_conversation_yields_nothing(self):
        assert build_extraction_input([]) == ""


class TestTheDeterministicTail:
    """What survives compaction without an LLM having to succeed."""

    def _messages(self):
        import json

        return [
            {"role": "user", "content": "do the task"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "/tmp_workspace/results/out.md"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"written": true}'},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c2",
                        "function": {
                            "name": "exec",
                            "arguments": json.dumps({"command": "ls -la"}),
                        },
                    }
                ],
            },
        ]

    def test_paths_written_this_run_survive(self):
        tail = deterministic_tail(self._messages())
        assert "/tmp_workspace/results/out.md" in tail

    def test_recent_tool_calls_survive(self):
        tail = deterministic_tail(self._messages())
        assert "exec" in tail and "write_file" in tail

    def test_it_is_bounded(self):
        import json

        many = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {
                            "name": "exec",
                            "arguments": json.dumps({"command": "x" * 500}),
                        },
                    }
                ],
            }
            for i in range(200)
        ]
        assert len(deterministic_tail(many)) < 4000

    def test_no_tool_activity_yields_empty(self):
        assert deterministic_tail([{"role": "user", "content": "hi"}]) == ""

    def test_malformed_tool_calls_do_not_raise(self):
        broken = [
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "assistant", "tool_calls": [{"function": {"arguments": "not json"}}]},
            {"role": "assistant", "tool_calls": "nonsense"},
        ]
        deterministic_tail(broken)


class TestTheTailSurvivesAFailedExtraction:
    """The point of a deterministic tail is that it does not need the LLM."""

    def test_retained_context_carries_the_tail_with_zero_facts(self):
        import json

        from robothor.engine.compaction import _build_retained_context_message

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "/tmp_workspace/results/out.md"}),
                        },
                    }
                ],
            }
        ]
        msg = _build_retained_context_message([], messages)
        assert "/tmp_workspace/results/out.md" in msg["content"], (
            "an LLM that returned nothing costs the run every path it wrote"
        )

    def test_facts_and_tail_coexist(self):
        import json

        from robothor.engine.compaction import CompactionFact, _build_retained_context_message

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "/out/a.md"}),
                        },
                    }
                ],
            }
        ]
        facts = [CompactionFact(text="grid is 15x15", category="context", priority=5)]
        content = _build_retained_context_message(facts, messages)["content"]
        assert "grid is 15x15" in content
        assert "/out/a.md" in content

    def test_the_call_still_works_without_messages(self):
        """Back-compat: existing callers pass facts only."""
        from robothor.engine.compaction import _build_retained_context_message

        msg = _build_retained_context_message([])
        assert msg["role"] == "user"
