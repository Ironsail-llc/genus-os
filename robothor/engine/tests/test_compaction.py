"""Tests for the enhanced context compaction system."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.compaction import (
    RETAINED_CONTEXT_MARKER,
    compact,
    extract_facts,
    extract_tool_summary,
    summarize_segment,
)

# ── TestExtractToolSummary ────────────────────────────────────────────


class TestExtractToolSummary:
    def test_json_single_key(self):
        content = json.dumps({"status": "ok" * 300})
        result = extract_tool_summary(content)
        assert "'status'" in result
        assert len(result) < len(content)

    def test_json_multiple_keys(self):
        data = {f"key_{i}": f"value_{i}" for i in range(10)}
        content = json.dumps(data) + " " * 500
        result = extract_tool_summary(content)
        assert "10 keys" in result

    def test_json_array(self):
        data = [{"id": i, "name": f"item_{i}"} for i in range(20)]
        content = json.dumps(data)
        result = extract_tool_summary(content)
        assert "20 items" in result

    def test_error_string(self):
        content = "Error: connection refused\n" + "stack trace line\n" * 50
        result = extract_tool_summary(content)
        assert "Error: connection refused" in result
        assert len(result) < len(content)

    def test_short_content_unchanged(self):
        content = "short result"
        result = extract_tool_summary(content)
        assert result == content

    def test_default_fallback(self):
        content = "x" * 600  # No JSON, no error keyword
        result = extract_tool_summary(content)
        assert result.endswith("...")
        assert len(result) <= 84  # 80 + "..."


# ── TestExtractFacts ──────────────────────────────────────────────────


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_valid_json_response(self):
        facts_json = json.dumps(
            {
                "facts": [
                    {"category": "decision", "text": "Use PostgreSQL", "priority": 5},
                    {"category": "pending", "text": "Update docs", "priority": 3},
                ]
            }
        )
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = facts_json

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            facts = await extract_facts(
                [
                    {"role": "user", "content": "Let's use PostgreSQL"},
                    {"role": "assistant", "content": "Done."},
                ]
            )

        assert len(facts) == 2
        assert facts[0].category == "decision"
        assert facts[0].text == "Use PostgreSQL"
        assert facts[0].priority == 5

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "not valid json {{"

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            facts = await extract_facts([{"role": "user", "content": "Hello"}])

        assert facts == []

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        with patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            facts = await extract_facts([{"role": "user", "content": "Hello"}])

        assert facts == []

    @pytest.mark.asyncio
    async def test_dedup_facts(self):
        facts_json = json.dumps(
            {
                "facts": [
                    {"category": "decision", "text": "Use PostgreSQL", "priority": 5},
                    {"category": "decision", "text": "Use PostgreSQL", "priority": 4},
                ]
            }
        )
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = facts_json

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            facts = await extract_facts([{"role": "user", "content": "test"}])

        assert len(facts) == 1

    @pytest.mark.asyncio
    async def test_empty_messages_returns_empty(self):
        facts = await extract_facts([])
        assert facts == []


# ── TestSummarizeSegment ──────────────────────────────────────────────


class TestSummarizeSegment:
    @pytest.mark.asyncio
    async def test_successful_summary(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Discussed database migration."

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await summarize_segment(
                [
                    {"role": "user", "content": "Migrate the database"},
                    {"role": "assistant", "content": "Done, migrated."},
                ]
            )

        assert "database migration" in result

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self):
        with patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=RuntimeError("fail"),
        ):
            result = await summarize_segment([{"role": "user", "content": "test"}])

        assert "Segment:" in result
        assert "compressed" in result

    @pytest.mark.asyncio
    async def test_empty_segment_fallback(self):
        result = await summarize_segment([])
        assert "Segment:" in result


# ── TestCompact ───────────────────────────────────────────────────────


def _make_large_conversation(n_pairs: int = 100, content_size: int = 3000) -> list[dict[str, Any]]:
    """Build a large conversation that exceeds compression threshold."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "System prompt."}]
    for i in range(n_pairs):
        messages.append({"role": "user", "content": f"Message {i} " + "x" * content_size})
        messages.append({"role": "assistant", "content": f"Response {i} " + "y" * content_size})
    return messages


class TestCompact:
    @pytest.mark.asyncio
    async def test_below_threshold_unchanged(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        result = await compact(messages, threshold=80_000)
        assert result.messages is messages
        assert result.passes_used == 0

    @pytest.mark.asyncio
    async def test_pass1_tool_thinning_sufficient(self):
        """When tool result clearing alone drops below drain_to."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
        ]
        # Add many large tool results to push over threshold
        for i in range(30):
            messages.append({"role": "user", "content": f"call {i}"})
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": "search", "arguments": "{}"},
                        }
                    ],
                },
            )
            messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * 5000})
        # Also add KEEP_RECENT messages at end
        messages.extend({"role": "user", "content": f"recent {i}"} for i in range(22))

        # Set thresholds so tool clearing alone is enough
        result = await compact(messages, threshold=100, drain_to=50_000)
        assert result.passes_used == 1
        # Tool results should be summarized, not raw
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        for m in tool_msgs[:5]:
            content = m.get("content", "")
            assert "[tool result:" in content or len(content) < 5000

    @pytest.mark.asyncio
    async def test_pass3_segmented_summary(self):
        """When fact extraction + segmented summary are needed."""
        messages = _make_large_conversation(n_pairs=60)

        facts_json = json.dumps(
            {"facts": [{"category": "decision", "text": "Chose PostgreSQL", "priority": 5}]}
        )
        mock_fact_resp = MagicMock()
        mock_fact_resp.choices = [MagicMock()]
        mock_fact_resp.choices[0].message.content = facts_json

        mock_summary_resp = MagicMock()
        mock_summary_resp.choices = [MagicMock()]
        mock_summary_resp.choices[0].message.content = "Segment summary text."

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            # First call is fact extraction (has response_format)
            if "response_format" in kwargs:
                return mock_fact_resp
            return mock_summary_resp

        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=mock_acompletion):
            result = await compact(messages, threshold=100, drain_to=50_000)

        assert result.passes_used >= 2
        assert len(result.facts_extracted) == 1
        assert result.facts_extracted[0].text == "Chose PostgreSQL"
        # System prompt preserved
        assert result.messages[0]["content"] == "System prompt."
        # Retained context present
        retained = [m for m in result.messages if RETAINED_CONTEXT_MARKER in m.get("content", "")]
        assert len(retained) == 1
        assert "Chose PostgreSQL" in retained[0]["content"]

    @pytest.mark.asyncio
    async def test_pass4_progressive_pruning(self):
        """When segmented summary is still too large, oldest segments get dropped."""
        messages = _make_large_conversation(n_pairs=100)

        facts_json = json.dumps(
            {"facts": [{"category": "context", "text": "Important fact", "priority": 4}]}
        )
        mock_fact_resp = MagicMock()
        mock_fact_resp.choices = [MagicMock()]
        mock_fact_resp.choices[0].message.content = facts_json

        # Make summaries large enough that pruning is needed
        mock_summary_resp = MagicMock()
        mock_summary_resp.choices = [MagicMock()]
        mock_summary_resp.choices[0].message.content = "Summary. " * 500

        async def mock_acompletion(**kwargs):
            if "response_format" in kwargs:
                return mock_fact_resp
            return mock_summary_resp

        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=mock_acompletion):
            result = await compact(messages, threshold=100, drain_to=500)

        assert result.passes_used == 4
        # Facts survive pruning
        retained = [m for m in result.messages if RETAINED_CONTEXT_MARKER in m.get("content", "")]
        assert len(retained) == 1
        assert "Important fact" in retained[0]["content"]

    @pytest.mark.asyncio
    async def test_retained_context_survives_recompaction(self):
        """Retained context messages from a previous compaction survive a second one."""
        # Simulate post-first-compaction state
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": f"{RETAINED_CONTEXT_MARKER}\n- [decision] (p5) Use PostgreSQL",
            },
            {
                "role": "user",
                "content": "[Conversation summary]\nPrevious work discussed.",
            },
            {
                "role": "assistant",
                "content": "Understood. I have context from our previous conversation.",
            },
        ]
        # Add enough new content to trigger compression again
        for i in range(60):
            messages.append({"role": "user", "content": f"New message {i} " + "x" * 3000})
            messages.append({"role": "assistant", "content": f"New response {i} " + "y" * 3000})

        facts_json = json.dumps({"facts": []})
        mock_fact_resp = MagicMock()
        mock_fact_resp.choices = [MagicMock()]
        mock_fact_resp.choices[0].message.content = facts_json

        mock_summary_resp = MagicMock()
        mock_summary_resp.choices = [MagicMock()]
        mock_summary_resp.choices[0].message.content = "Second compaction summary."

        async def mock_acompletion(**kwargs):
            if "response_format" in kwargs:
                return mock_fact_resp
            return mock_summary_resp

        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=mock_acompletion):
            result = await compact(messages, threshold=100, drain_to=50_000)

        # Original retained facts should still be present
        retained = [m for m in result.messages if RETAINED_CONTEXT_MARKER in m.get("content", "")]
        assert len(retained) == 1
        assert "Use PostgreSQL" in retained[0]["content"]

    @pytest.mark.asyncio
    async def test_too_few_messages_unchanged(self):
        """Conversations with <= KEEP_RECENT+1 messages aren't compacted."""
        messages = [
            {"role": "system", "content": "x" * 400_000},  # Over threshold by content
        ]
        result = await compact(messages, threshold=100)
        assert result.passes_used == 0
        assert result.messages is messages

    @pytest.mark.asyncio
    async def test_output_shape_backward_compat(self):
        """CompactionResult.messages is a list of dicts — same shape as old maybe_compress."""
        messages = _make_large_conversation(n_pairs=40)

        facts_json = json.dumps({"facts": []})
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = facts_json

        mock_summary = MagicMock()
        mock_summary.choices = [MagicMock()]
        mock_summary.choices[0].message.content = "Summary."

        async def mock_acompletion(**kwargs):
            if "response_format" in kwargs:
                return mock_resp
            return mock_summary

        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=mock_acompletion):
            result = await compact(messages, threshold=100, drain_to=50_000)

        assert isinstance(result.messages, list)
        for msg in result.messages:
            assert isinstance(msg, dict)
            assert "role" in msg
