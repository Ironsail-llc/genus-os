"""
Periodic Analysis (Tier 2) — Test Suite

Run:
    cd ~/clawd/memory_system && ./venv/bin/python -m pytest test_periodic_analysis.py -v -m "not integration"
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    client.generate = AsyncMock(return_value="mocked LLM response")
    return client


# ═══════════════════════════════════════════════════════════════════
# Meeting Prep
# ═══════════════════════════════════════════════════════════════════


class TestMeetingPrep:
    @pytest.mark.asyncio
    async def test_no_calendar_file(self, mock_llm_client):
        """Returns empty results when calendar-log.json doesn't exist."""
        from periodic_analysis import meeting_prep

        with patch("periodic_analysis.MEMORY_DIR") as mock_dir:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await meeting_prep(mock_llm_client)

        assert results["meetings_found"] == 0
        assert results["briefs_generated"] == 0

    @pytest.mark.asyncio
    async def test_skips_past_meetings(self, mock_llm_client):
        """Meetings in the past are not included."""
        from periodic_analysis import meeting_prep

        past = (datetime.now() - timedelta(hours=2)).isoformat()
        calendar = {
            "entries": {
                "evt1": {"summary": "Past Meeting", "start": past},
            }
        }

        with patch("periodic_analysis.MEMORY_DIR") as mock_dir:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = json.dumps(calendar)
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await meeting_prep(mock_llm_client)

        assert results["meetings_found"] == 0

    @pytest.mark.asyncio
    async def test_includes_upcoming_meeting(self, mock_llm_client):
        """Meetings in the next 6 hours are included."""
        from periodic_analysis import meeting_prep

        future = (datetime.now() + timedelta(hours=2)).isoformat()
        calendar = {
            "entries": {
                "evt1": {
                    "summary": "Sprint Review",
                    "start": future,
                    "attendees": ["alice@example.com"],
                },
            }
        }

        mock_ingest = AsyncMock(return_value={"fact_ids": [1]})

        with (
            patch("periodic_analysis.MEMORY_DIR") as mock_dir,
            patch("ingestion.ingest_content", mock_ingest),
            patch("crm_fetcher.fetch_all_contacts", side_effect=Exception("no CRM")),
        ):
            mock_cal = MagicMock()
            mock_cal.exists.return_value = True
            mock_cal.read_text.return_value = json.dumps(calendar)

            mock_email = MagicMock()
            mock_email.exists.return_value = False

            def truediv_side_effect(name):
                if "calendar" in str(name):
                    return mock_cal
                return mock_email

            mock_dir.__truediv__ = MagicMock(side_effect=truediv_side_effect)

            results = await meeting_prep(mock_llm_client)

        assert results["meetings_found"] == 1
        assert results["briefs_generated"] == 1
        mock_llm_client.generate.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# Memory Blocks
# ═══════════════════════════════════════════════════════════════════


class TestMemoryBlocks:
    @pytest.mark.asyncio
    async def test_updates_blocks_with_data(self, mock_llm_client):
        """Blocks are updated when relevant facts exist."""
        from periodic_analysis import memory_blocks

        mock_rows = [{"fact_text": "Engagement score for Alice: active"}]

        with (
            patch("periodic_analysis.psycopg2.connect") as mock_connect,
            patch("periodic_analysis.MEMORY_DIR") as mock_dir,
        ):
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = mock_rows
            mock_connect.return_value.cursor.return_value = mock_cur
            mock_connect.return_value.close = MagicMock()
            mock_connect.return_value.commit = MagicMock()

            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await memory_blocks(mock_llm_client)

        assert results["blocks_updated"] >= 1


# ═══════════════════════════════════════════════════════════════════
# Entity Enrichment
# ═══════════════════════════════════════════════════════════════════


class TestEntityEnrichment:
    @pytest.mark.asyncio
    async def test_no_unlinked_facts(self):
        """No work when all facts have entities."""
        from periodic_analysis import entity_enrichment

        with patch("periodic_analysis.psycopg2.connect") as mock_connect:
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = []
            mock_connect.return_value.cursor.return_value = mock_cur
            mock_connect.return_value.close = MagicMock()

            results = await entity_enrichment()

        assert results["facts_processed"] == 0

    @pytest.mark.asyncio
    async def test_processes_unlinked_facts(self):
        """Unlinked facts get entity extraction."""
        from periodic_analysis import entity_enrichment

        with (
            patch("periodic_analysis.psycopg2.connect") as mock_connect,
            patch("entity_graph.extract_entities_batch", new_callable=AsyncMock) as mock_extract,
        ):
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [{"id": 1}, {"id": 2}]
            mock_connect.return_value.cursor.return_value = mock_cur
            mock_connect.return_value.close = MagicMock()

            mock_extract.return_value = {"entities_stored": 3, "relations_stored": 1}

            results = await entity_enrichment()

        assert results["facts_processed"] == 2
        assert results["entities_added"] == 3
        mock_extract.assert_called_once_with([1, 2])
