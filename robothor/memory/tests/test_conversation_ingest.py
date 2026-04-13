"""Tests for conversation session ingestion into memory pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory.conversation_ingest import (
    MAX_TRANSCRIPT_MESSAGES,
    MIN_HISTORY_THRESHOLD,
    format_transcript,
    ingest_conversation_session,
)


class TestFormatTranscript:
    def test_formats_user_and_assistant_messages(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = format_transcript(history)
        assert "User: Hello" in result
        assert "Assistant: Hi there" in result

    def test_preserves_message_order(self):
        history = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Second"},
            {"role": "user", "content": "Third"},
        ]
        result = format_transcript(history)
        lines = [ln for ln in result.strip().split("\n") if ln.strip()]
        assert lines[0] == "User: First"
        assert lines[1] == "Assistant: Second"
        assert lines[2] == "User: Third"

    def test_truncates_to_max_messages(self):
        history = [
            {"role": "user", "content": f"Message {i}"} for i in range(MAX_TRANSCRIPT_MESSAGES + 10)
        ]
        result = format_transcript(history)
        lines = [ln for ln in result.strip().split("\n") if ln.strip()]
        assert len(lines) == MAX_TRANSCRIPT_MESSAGES

    def test_truncation_keeps_most_recent(self):
        history = [
            {"role": "user", "content": f"Message {i}"} for i in range(MAX_TRANSCRIPT_MESSAGES + 5)
        ]
        result = format_transcript(history)
        assert f"Message {MAX_TRANSCRIPT_MESSAGES + 4}" in result
        assert "Message 0" not in result

    def test_handles_system_role_gracefully(self):
        history = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = format_transcript(history)
        assert "System prompt" not in result
        assert "User: Hello" in result

    def test_handles_empty_content(self):
        history = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Response"},
        ]
        result = format_transcript(history)
        assert "Assistant: Response" in result

    def test_handles_failed_run_markers(self):
        history = [
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "[Run failed: timeout]"},
        ]
        result = format_transcript(history)
        assert "[Run failed: timeout]" in result


class TestThresholdFiltering:
    @pytest.mark.asyncio
    async def test_skips_short_conversations(self):
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        result = await ingest_conversation_session(
            session_key="telegram:123",
            history=history,
            agent_id="main",
            trigger_type="telegram",
            run_id="run-001",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_empty_history(self):
        result = await ingest_conversation_session(
            session_key="telegram:123",
            history=[],
            agent_id="main",
            trigger_type="telegram",
            run_id="run-001",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_threshold_is_configurable_constant(self):
        assert MIN_HISTORY_THRESHOLD == 4


class TestDedup:
    @pytest.mark.asyncio
    async def test_skips_already_ingested_session(self):
        history = [{"role": "user", "content": f"Message {i}"} for i in range(6)]
        with patch(
            "robothor.memory.conversation_ingest.is_already_ingested",
            return_value=True,
        ):
            result = await ingest_conversation_session(
                session_key="telegram:123",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-001",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_ingests_when_not_yet_ingested(self):
        history = [{"role": "user", "content": f"Msg {i}"} for i in range(6)]
        mock_result = {
            "source_channel": "telegram",
            "content_type": "conversation",
            "facts_processed": 2,
            "facts_skipped": 0,
            "fact_ids": [10, 11],
            "entities_stored": 1,
            "relations_stored": 0,
        }
        with (
            patch("robothor.memory.conversation_ingest.is_already_ingested", return_value=False),
            patch(
                "robothor.memory.conversation_ingest.ingest_content",
                new_callable=AsyncMock,
                return_value=mock_result,
            ) as mock_ingest,
            patch("robothor.memory.conversation_ingest.record_ingested") as mock_record,
        ):
            result = await ingest_conversation_session(
                session_key="telegram:123",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-001",
            )
            assert result is not None
            assert result["facts_processed"] == 2
            mock_ingest.assert_called_once()
            mock_record.assert_called_once()

    @pytest.mark.asyncio
    async def test_hash_changes_when_conversation_grows(self):
        from robothor.memory.conversation_ingest import _compute_session_hash

        history_short = [{"role": "user", "content": f"Msg {i}"} for i in range(4)]
        history_long = history_short + [
            {"role": "user", "content": "New message"},
            {"role": "assistant", "content": "New response"},
        ]
        assert _compute_session_hash("telegram:123", history_short) != _compute_session_hash(
            "telegram:123", history_long
        )

    @pytest.mark.asyncio
    async def test_same_conversation_produces_same_hash(self):
        from robothor.memory.conversation_ingest import _compute_session_hash

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "Good!"},
        ]
        assert _compute_session_hash("telegram:123", history) == _compute_session_hash(
            "telegram:123", history
        )


class TestIngestContentCall:
    @pytest.mark.asyncio
    async def test_passes_correct_source_channel_telegram(self):
        history = [{"role": "user", "content": f"M{i}"} for i in range(6)]
        with (
            patch("robothor.memory.conversation_ingest.is_already_ingested", return_value=False),
            patch(
                "robothor.memory.conversation_ingest.ingest_content",
                new_callable=AsyncMock,
                return_value={"fact_ids": [1], "facts_processed": 1, "facts_skipped": 0},
            ) as mock_ingest,
            patch("robothor.memory.conversation_ingest.record_ingested"),
        ):
            await ingest_conversation_session(
                session_key="telegram:456",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-002",
            )
            assert mock_ingest.call_args[1]["source_channel"] == "telegram"
            assert mock_ingest.call_args[1]["content_type"] == "conversation"

    @pytest.mark.asyncio
    async def test_passes_correct_source_channel_webchat(self):
        history = [{"role": "user", "content": f"M{i}"} for i in range(6)]
        with (
            patch("robothor.memory.conversation_ingest.is_already_ingested", return_value=False),
            patch(
                "robothor.memory.conversation_ingest.ingest_content",
                new_callable=AsyncMock,
                return_value={"fact_ids": [1], "facts_processed": 1, "facts_skipped": 0},
            ) as mock_ingest,
            patch("robothor.memory.conversation_ingest.record_ingested"),
        ):
            await ingest_conversation_session(
                session_key="agent:main:web",
                history=history,
                agent_id="main",
                trigger_type="webchat",
                run_id="run-003",
            )
            assert mock_ingest.call_args[1]["source_channel"] == "webchat"

    @pytest.mark.asyncio
    async def test_metadata_includes_session_key_and_run_id(self):
        history = [{"role": "user", "content": f"M{i}"} for i in range(6)]
        with (
            patch("robothor.memory.conversation_ingest.is_already_ingested", return_value=False),
            patch(
                "robothor.memory.conversation_ingest.ingest_content",
                new_callable=AsyncMock,
                return_value={"fact_ids": [], "facts_processed": 0, "facts_skipped": 0},
            ) as mock_ingest,
            patch("robothor.memory.conversation_ingest.record_ingested"),
        ):
            await ingest_conversation_session(
                session_key="telegram:789",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-004",
            )
            metadata = mock_ingest.call_args[1]["metadata"]
            assert metadata["session_key"] == "telegram:789"
            assert metadata["run_id"] == "run-004"
            assert metadata["agent_id"] == "main"


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_ingestion_failure_returns_none_no_raise(self):
        history = [{"role": "user", "content": f"M{i}"} for i in range(6)]
        with (
            patch("robothor.memory.conversation_ingest.is_already_ingested", return_value=False),
            patch(
                "robothor.memory.conversation_ingest.ingest_content",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection failed"),
            ),
        ):
            result = await ingest_conversation_session(
                session_key="telegram:123",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-005",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_dedup_check_failure_returns_none(self):
        history = [{"role": "user", "content": f"M{i}"} for i in range(6)]
        with patch(
            "robothor.memory.conversation_ingest.is_already_ingested",
            side_effect=Exception("Redis down"),
        ):
            result = await ingest_conversation_session(
                session_key="telegram:123",
                history=history,
                agent_id="main",
                trigger_type="telegram",
                run_id="run-006",
            )
            assert result is None
