"""
Ingest Service — Tests

Tests for the transcript/content ingestion pipeline:
- Transcript detection and parsing
- Embedding generation and storage in pgvector
- Idempotency (duplicate content → skipped)
- Malformed input handling
- Searchability of ingested content via rag.py

These tests exercise the full ingest path from raw content to stored,
searchable vectors — the entry point for all data entering the memory system.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg2
import pytest
from fact_extraction import search_facts, store_fact
from ingestion import ingest_content
from rag import get_embedding, search_all_memory, search_short_term, store_short_term

# ============== Unit Tests: Transcript Parsing ==============


class TestTranscriptParsing:
    """Tests for parsing JSONL session transcripts."""

    def test_parse_valid_jsonl_session(self):
        """A valid JSONL session file should produce messages."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_001",
                        "message": {
                            "role": "user",
                            "content": "What is the capital of France? I need to know for my trip.",
                        },
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_002",
                        "message": {
                            "role": "assistant",
                            "content": "The capital of France is Paris. It has many famous landmarks.",
                        },
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert len(messages) == 2
            assert messages[0]["role"] == "user"
            assert "France" in messages[0]["content"]
            assert messages[1]["role"] == "assistant"
        finally:
            path.unlink()

    def test_parse_skips_short_messages(self):
        """Messages under 10 chars should be skipped."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_short",
                        "message": {"role": "user", "content": "hi"},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert len(messages) == 0
        finally:
            path.unlink()

    def test_parse_skips_commands(self):
        """Messages starting with / should be skipped."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_cmd",
                        "message": {"role": "user", "content": "/model gemini-pro"},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert len(messages) == 0
        finally:
            path.unlink()

    def test_parse_skips_non_message_entries(self):
        """Non-message entries (tool_result, system) should be skipped."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "tool_result",
                        "id": "tr_001",
                        "result": {"output": "some tool output here"},
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_sys",
                        "message": {"role": "system", "content": "You are a helpful assistant."},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert len(messages) == 0
        finally:
            path.unlink()

    def test_parse_handles_malformed_json_lines(self):
        """Malformed JSON lines should be skipped, not crash."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("this is not valid json\n")
            f.write("{incomplete json\n")
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_valid",
                        "message": {
                            "role": "user",
                            "content": "This is a valid message that should be parsed correctly.",
                        },
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            # Should get only the valid message, not crash
            assert len(messages) == 1
            assert "valid message" in messages[0]["content"]
        finally:
            path.unlink()

    def test_parse_handles_empty_file(self):
        """An empty file should return an empty list."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert messages == []
        finally:
            path.unlink()

    def test_parse_handles_array_content_format(self):
        """Content as array of {type, text} objects should be parsed."""
        from transcript_sync import parse_session_file

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "msg_array",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Here is a detailed response to your question about AI.",
                                },
                            ],
                        },
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        try:
            messages = parse_session_file(path)
            assert len(messages) == 1
            assert "detailed response" in messages[0]["content"]
        finally:
            path.unlink()


# ============== Tests: Embedding Generation & Storage ==============


class TestEmbeddingStorage:
    """Tests for embedding generation and pgvector storage."""

    def test_get_embedding_returns_1024_dim(self):
        """Qwen3-embedding:0.6b should produce 1024-dimensional vectors."""
        embedding = get_embedding("Test embedding dimensionality check")
        assert isinstance(embedding, list)
        assert len(embedding) == 1024
        # Check it's actually numeric
        assert all(isinstance(x, (int, float)) for x in embedding)

    def test_store_short_term_generates_embedding(self, test_prefix):
        """Storing in short-term memory should create an embedding."""
        content = f"{test_prefix} Robothor uses pgvector for semantic search"
        mem_id = store_short_term(content, "test")
        assert isinstance(mem_id, int)
        assert mem_id > 0

        conn = psycopg2.connect(
            **{
                "dbname": "robothor_memory",
                "user": "philip",
                "host": "/var/run/postgresql",
            }
        )
        cur = conn.cursor()
        cur.execute("SELECT embedding FROM short_term_memory WHERE id = %s", (mem_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None  # Embedding exists
        conn.close()

    async def test_store_fact_generates_embedding(self, test_prefix, db_conn):
        """Storing a fact should create an embedding in memory_facts."""
        fact = {
            "fact_text": f"{test_prefix} DGX Spark has 128GB unified memory",
            "category": "technical",
            "entities": ["DGX Spark"],
            "confidence": 0.95,
        }
        fact_id = await store_fact(fact, f"{test_prefix} source", "test")
        assert isinstance(fact_id, int)

        cur = db_conn.cursor()
        cur.execute("SELECT embedding FROM memory_facts WHERE id = %s", (fact_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None

    def test_different_texts_produce_different_embeddings(self):
        """Semantically different texts should produce different embeddings."""
        emb1 = get_embedding("Python programming language features")
        emb2 = get_embedding("Italian pasta recipes for dinner")
        # Not identical (floating point, so exact equality would be surprising)
        assert emb1 != emb2


# ============== Tests: Idempotency ==============


class TestIdempotency:
    """Tests that duplicate content is handled correctly."""

    async def test_ingest_duplicate_content_detected(self, test_prefix):
        """Ingesting identical content should detect duplicates via conflict resolution."""
        content = f"{test_prefix} Philip bought a new mechanical keyboard"
        mock_facts = [
            {
                "fact_text": f"{test_prefix} Philip bought a new mechanical keyboard",
                "category": "event",
                "entities": ["Philip"],
                "confidence": 0.9,
            }
        ]

        # First ingest — should store
        with (
            patch(
                "ingestion.extract_facts",
                new_callable=AsyncMock,
                return_value=mock_facts,
            ),
            patch(
                "conflict_resolution.resolve_and_store",
                new_callable=AsyncMock,
                return_value={"new_id": 99998, "action": "stored"},
            ),
            patch(
                "entity_graph.extract_and_store_entities",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result1 = await ingest_content(
                content=content,
                source_channel="test",
                content_type="conversation",
            )
        assert result1["facts_processed"] >= 1

        # Second ingest of same content — conflict resolution should detect duplicate
        with (
            patch(
                "ingestion.extract_facts",
                new_callable=AsyncMock,
                return_value=mock_facts,
            ),
            patch(
                "conflict_resolution.resolve_and_store",
                new_callable=AsyncMock,
                return_value={"new_id": None, "action": "duplicate_skipped"},
            ),
            patch(
                "entity_graph.extract_and_store_entities",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result2 = await ingest_content(
                content=content,
                source_channel="test",
                content_type="conversation",
            )
        # Duplicate should be skipped
        assert result2["facts_skipped"] >= 1 or result2["facts_processed"] == 0

    def test_store_short_term_allows_duplicates_but_ids_differ(self, test_prefix):
        """Short-term memory allows duplicate content (different IDs)."""
        content = f"{test_prefix} Duplicate content test"
        id1 = store_short_term(content, "test")
        id2 = store_short_term(content, "test")
        assert id1 != id2  # Each store creates a new entry


# ============== Tests: Malformed Input Handling ==============


class TestMalformedInputHandling:
    """Tests that malformed/bad input is handled gracefully."""

    async def test_ingest_empty_content_raises(self):
        """Empty content should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await ingest_content(
                content="",
                source_channel="test",
                content_type="conversation",
            )

    async def test_ingest_whitespace_only_raises(self):
        """Whitespace-only content should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await ingest_content(
                content="   \n\t  ",
                source_channel="test",
                content_type="conversation",
            )

    async def test_ingest_llm_failure_returns_empty(self, test_prefix):
        """If the LLM fails during extraction, ingestion should still succeed with 0 facts."""
        with patch(
            "ingestion.extract_facts",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await ingest_content(
                content=f"{test_prefix} Some content that triggers LLM failure",
                source_channel="test",
                content_type="conversation",
            )
        assert result["facts_processed"] == 0

    def test_parse_nonexistent_file(self):
        """Parsing a nonexistent file should return empty list, not crash."""
        from transcript_sync import parse_session_file

        result = parse_session_file(Path("/tmp/nonexistent_session_file_xyz.jsonl"))
        assert result == []


# ============== Tests: Searchability of Ingested Content ==============


class TestSearchability:
    """Tests that ingested content is searchable via rag.py."""

    def test_stored_short_term_is_searchable(self, test_prefix):
        """Content stored in short-term memory should be found by semantic search."""
        content = f"{test_prefix} The ThinkStation PGX has dual NVIDIA GPUs"
        store_short_term(content, "technical")

        results = search_short_term(f"{test_prefix} ThinkStation GPU setup", limit=5)
        assert len(results) >= 1
        assert any(test_prefix in r["content"] for r in results)

    def test_search_all_memory_finds_stored_content(self, test_prefix):
        """search_all_memory should find content across tiers."""
        content = f"{test_prefix} Robothor uses Qwen3-Embedding for vectors"
        store_short_term(content, "technical")

        results = search_all_memory(f"{test_prefix} Qwen3 embedding model", limit=10)
        assert len(results) >= 1
        assert any(test_prefix in r["content"] for r in results)

    async def test_stored_fact_is_searchable(self, test_prefix):
        """Facts stored via store_fact should be searchable via search_facts."""
        fact = {
            "fact_text": f"{test_prefix} Philip uses Catppuccin Mocha color scheme",
            "category": "preference",
            "entities": ["Philip", "Catppuccin"],
            "confidence": 0.9,
        }
        await store_fact(fact, f"{test_prefix} source", "conversation")

        results = await search_facts(f"{test_prefix} Catppuccin color scheme preference", limit=5)
        assert len(results) >= 1
        assert any(test_prefix in r["fact_text"] for r in results)

    async def test_ingested_content_creates_searchable_facts(self, test_prefix):
        """Full ingest pipeline should produce facts searchable via search_facts."""
        content = f"{test_prefix} Philip migrated the database to PostgreSQL 16"
        mock_facts = [
            {
                "fact_text": f"{test_prefix} Philip migrated to PostgreSQL 16",
                "category": "decision",
                "entities": ["Philip", "PostgreSQL"],
                "confidence": 0.9,
            }
        ]

        with (
            patch(
                "ingestion.extract_facts",
                new_callable=AsyncMock,
                return_value=mock_facts,
            ),
            patch(
                "entity_graph.extract_and_store_entities",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "conflict_resolution.find_similar_facts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await ingest_content(
                content=content,
                source_channel="test",
                content_type="decision",
            )
        assert result["facts_processed"] >= 1

        # Now search for it
        search_results = await search_facts(f"{test_prefix} PostgreSQL migration", limit=5)
        assert len(search_results) >= 1
        assert any(test_prefix in r["fact_text"] for r in search_results)


# ============== Cleanup Fixture for Short-Term Memory ==============


@pytest.fixture(autouse=True)
def cleanup_short_term(test_prefix):
    """Clean up short-term memory entries created during tests."""
    yield
    try:
        conn = psycopg2.connect(
            dbname="robothor_memory",
            user="philip",
            host="/var/run/postgresql",
        )
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM short_term_memory WHERE content LIKE %s",
            (f"%{test_prefix}%",),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
