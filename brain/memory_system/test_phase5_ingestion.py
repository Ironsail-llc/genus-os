"""
Phase 5: Cross-Channel Ingestion — Tests

Tests for ingesting content from multiple channels (discord, email, cli, api, telegram)
with fact extraction and conflict resolution.
"""

from unittest.mock import AsyncMock, patch

import pytest
from ingestion import ingest_content

# ============== Unit Tests ==============


class TestIngestion:
    @pytest.mark.asyncio
    async def test_ingest_preserves_source_channel(self, test_prefix, db_conn):
        from psycopg2.extras import RealDictCursor

        with (
            patch(
                "ingestion.extract_facts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "fact_text": f"{test_prefix} test channel fact",
                        "category": "personal",
                        "entities": [],
                        "confidence": 0.9,
                    }
                ],
            ),
            patch(
                "entity_graph.extract_and_store_entities", new_callable=AsyncMock, return_value={}
            ),
        ):
            result = await ingest_content(
                content=f"{test_prefix} some content",
                source_channel="discord",
                content_type="conversation",
            )

        assert result["source_channel"] == "discord"
        assert result["facts_processed"] >= 1

        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT source_channel FROM memory_facts WHERE fact_text LIKE %s",
            (f"%{test_prefix}%",),
        )
        rows = cur.fetchall()
        assert any(r["source_channel"] == "discord" for r in rows)

    @pytest.mark.asyncio
    async def test_ingest_from_different_channels(self, test_prefix):
        channels = ["discord", "email", "cli", "api", "telegram"]
        for channel in channels:
            with (
                patch(
                    "ingestion.extract_facts",
                    new_callable=AsyncMock,
                    return_value=[
                        {
                            "fact_text": f"{test_prefix} fact from {channel}",
                            "category": "personal",
                            "entities": [],
                            "confidence": 0.8,
                        }
                    ],
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
                    content=f"{test_prefix} content from {channel}",
                    source_channel=channel,
                    content_type="conversation",
                )
                assert result["source_channel"] == channel

    @pytest.mark.asyncio
    async def test_ingest_with_metadata(self, test_prefix):
        metadata = {"sender": "philip@ironsail.ai", "thread_id": "abc123"}
        mock_fact = {
            "fact_text": f"{test_prefix} metadata test",
            "category": "personal",
            "entities": [],
            "confidence": 0.9,
        }
        with (
            patch("ingestion.extract_facts", new_callable=AsyncMock, return_value=[mock_fact]),
            patch(
                "conflict_resolution.resolve_and_store",
                new_callable=AsyncMock,
                return_value={"new_id": 99999, "action": "stored"},
            ),
            patch(
                "entity_graph.extract_and_store_entities", new_callable=AsyncMock, return_value={}
            ),
        ):
            result = await ingest_content(
                content=f"{test_prefix} email content",
                source_channel="email",
                content_type="email",
                metadata=metadata,
            )
        assert result["facts_processed"] >= 1

    @pytest.mark.asyncio
    async def test_ingest_empty_content_rejected(self, test_prefix):
        with pytest.raises(ValueError, match="empty"):
            await ingest_content(
                content="",
                source_channel="api",
                content_type="conversation",
            )

    @pytest.mark.asyncio
    async def test_ingest_runs_fact_extraction(self, test_prefix):
        with (
            patch(
                "ingestion.extract_facts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "fact_text": f"{test_prefix} extracted fact",
                        "category": "decision",
                        "entities": [],
                        "confidence": 0.95,
                    }
                ],
            ) as mock_extract,
            patch(
                "entity_graph.extract_and_store_entities", new_callable=AsyncMock, return_value={}
            ),
        ):
            await ingest_content(
                content=f"{test_prefix} Philip decided to use Qwen3",
                source_channel="cli",
                content_type="decision",
            )
            mock_extract.assert_called_once()

    def test_orchestrator_ingest_endpoint(self):
        """Verify the /ingest endpoint is registered in the orchestrator."""
        from orchestrator import app

        routes = [r.path for r in app.routes]
        assert "/ingest" in routes


# ============== Integration Tests (Real LLM) ==============


@pytest.mark.slow
class TestIntegrationIngestion:
    @pytest.mark.asyncio
    async def test_ingest_discord_message(self, test_prefix):
        result = await ingest_content(
            content=f"{test_prefix} Philip mentioned he's working on the memory system upgrade with Qwen3-Next",
            source_channel="discord",
            content_type="conversation",
        )
        assert result["facts_processed"] >= 1
        assert result["source_channel"] == "discord"
