"""
Intelligence Pipeline (Tier 3) — Tests

Tests for the deep analysis pipeline. Ingestion tests are now in
test_continuous_ingest.py, periodic analysis in test_periodic_analysis.py.

This file covers:
- Phase 2: Relationship Intelligence (kept from old Phase 6)
- Phase 3: Engagement Scoring (kept from old Phase 7)
- Phase 4: Pattern Detection (kept from old Phase 9)
- Phase 5: Quality Scoring (kept from old Phase 12)
- Phase 6: Cleanup
- Pipeline report structure

Run unit tests:
    cd ~/robothor/brain/memory_system && ./venv/bin/python -m pytest test_intelligence_pipeline.py -v -m "not integration and not llm"

Run integration tests:
    cd ~/robothor/brain/memory_system && ./venv/bin/python -m pytest test_intelligence_pipeline.py -v -m integration
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_llm_client():
    """Mock LLM client that returns predictable responses."""
    client = MagicMock()
    client.generate = AsyncMock(return_value="mocked LLM response")
    return client


# ═══════════════════════════════════════════════════════════════════
# Phase 3: Engagement Scoring — Pipe Response Parsing
# ═══════════════════════════════════════════════════════════════════


class TestPhase3EngagementScoring:
    """Tests for engagement scoring response parsing."""

    @pytest.mark.asyncio
    async def test_parses_pipe_response(self):
        """Standard 'Name | level | reason' lines should be parsed correctly."""
        from intelligence_pipeline import phase_3_engagement_scoring

        mock_response = """Alice Smith | active | 3 emails this week
Bob Jones | dormant | No activity in 45 days
Carol Davis | high | Daily Slack + 2 meetings"""

        mock_client = MagicMock()
        mock_client.generate = AsyncMock(return_value=mock_response)

        mock_contacts = [
            {
                "firstName": "Alice",
                "lastName": "Smith",
                "email": "alice@test.com",
                "updatedAt": datetime.now().isoformat(),
            },
            {
                "firstName": "Bob",
                "lastName": "Jones",
                "email": "bob@test.com",
                "updatedAt": datetime.now().isoformat(),
            },
        ]

        with (
            patch("intelligence_pipeline.psycopg2") as mock_pg,
            patch.dict(
                "sys.modules",
                {
                    "crm_fetcher": MagicMock(
                        fetch_all_contacts=MagicMock(return_value=mock_contacts),
                        fetch_conversations=MagicMock(return_value=[]),
                    ),
                },
            ),
            patch("intelligence_pipeline.MEMORY_DIR", Path("/tmp/nonexistent")),
        ):
            # Mock DB cursor
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            mock_pg.connect.return_value = mock_conn

            results = await phase_3_engagement_scoring(mock_client)

        assert results["scored"] >= 2  # Alice (active) and Bob (dormant) are valid levels

    @pytest.mark.asyncio
    async def test_handles_malformed_lines(self):
        """Lines without pipes or with invalid levels should be skipped."""
        from intelligence_pipeline import phase_3_engagement_scoring

        mock_response = """This is a header line
Alice Smith | active | reason
This line has no pipes at all
Bob | invalid_level | reason
Carol | low | valid reason"""

        mock_client = MagicMock()
        mock_client.generate = AsyncMock(return_value=mock_response)

        mock_contacts = [
            {
                "firstName": "Alice",
                "lastName": "Smith",
                "email": "a@test.com",
                "updatedAt": datetime.now().isoformat(),
            },
        ]

        with (
            patch("intelligence_pipeline.psycopg2") as mock_pg,
            patch.dict(
                "sys.modules",
                {
                    "crm_fetcher": MagicMock(
                        fetch_all_contacts=MagicMock(return_value=mock_contacts),
                        fetch_conversations=MagicMock(return_value=[]),
                    ),
                },
            ),
            patch("intelligence_pipeline.MEMORY_DIR", Path("/tmp/nonexistent")),
        ):
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            mock_pg.connect.return_value = mock_conn

            results = await phase_3_engagement_scoring(mock_client)

        # "active" and "low" are valid, "invalid_level" is not
        assert results["scored"] == 2


# ═══════════════════════════════════════════════════════════════════
# Phase 4: Pattern Detection — Priority Tags
# ═══════════════════════════════════════════════════════════════════


class TestPhase4PatternDetection:
    """Tests for pattern detection priority tag extraction."""

    @pytest.mark.asyncio
    async def test_extracts_priority_tags(self, tmp_path):
        """[HIGH], [MED], [LOW] tags should be extracted from pattern lines."""
        from intelligence_pipeline import phase_4_pattern_detection

        mock_response = """[HIGH] Critical: Payment system outage reported by 3 contacts
[MED] Theme: Multiple conversations about Q1 planning
[LOW] Minor: Newsletter engagement declining slightly"""

        mock_client = MagicMock()
        mock_client.generate = AsyncMock(return_value=mock_response)

        # Write minimal email log
        email_path = tmp_path / "email-log.json"
        email_path.write_text(
            json.dumps(
                {
                    "entries": {
                        "e1": {
                            "from": "test@test.com",
                            "subject": "Test",
                            "processedAt": datetime.now().isoformat(),
                            "urgency": "medium",
                        },
                    }
                }
            )
        )

        stored_metadata = []

        with (
            patch("intelligence_pipeline.MEMORY_DIR", tmp_path),
            patch("intelligence_pipeline.psycopg2") as mock_pg,
            patch.dict(
                "sys.modules",
                {
                    "crm_fetcher": MagicMock(
                        fetch_conversations=MagicMock(return_value=[]),
                    ),
                },
            ),
        ):
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value = mock_cursor

            def capture_execute(sql, params=None):
                if params and len(params) >= 6:
                    meta = json.loads(params[5]) if isinstance(params[5], str) else {}
                    stored_metadata.append(meta)

            mock_cursor.execute = capture_execute
            mock_pg.connect.return_value = mock_conn

            results = await phase_4_pattern_detection(mock_client)

        assert results["patterns_found"] == 3
        priorities = [m.get("priority") for m in stored_metadata]
        assert "high" in priorities
        assert "medium" in priorities
        assert "low" in priorities


# ═══════════════════════════════════════════════════════════════════
# Phase 5: Quality Scoring — JSON Schema
# ═══════════════════════════════════════════════════════════════════


class TestPhase5QualityScoring:
    """Tests for improved phase 5 quality scoring."""

    @pytest.mark.asyncio
    async def test_json_score_parsing(self, mock_llm_client):
        """Quality scoring should parse JSON format: {"score": N, "reason": "..."}."""
        from intelligence_pipeline import phase_5_housekeeping

        mock_llm_client.generate = AsyncMock(
            return_value='{"score": 4, "reason": "Results are relevant and recent"}'
        )

        mock_search_results = [
            {"content": "Recent decision about database migration"},
            {"content": "Contact update for Alice Smith"},
        ]

        with patch.dict(
            "sys.modules",
            {
                "lifecycle": MagicMock(
                    run_lifecycle_maintenance=AsyncMock(
                        return_value={"facts_scored": 5, "decay_updated": 3}
                    ),
                ),
                "rag": MagicMock(
                    search_all_memory=MagicMock(return_value=mock_search_results),
                ),
            },
        ):
            results = await phase_5_housekeeping(mock_llm_client)

        quality = results.get("quality", {})
        assert quality.get("average_score", 0) > 0
        # All test queries should have scores
        assert len(quality.get("tests", [])) >= 3

    @pytest.mark.asyncio
    async def test_json_score_fallback_on_bad_json(self, mock_llm_client):
        """If LLM returns non-JSON, should fall back to string parsing."""
        from intelligence_pipeline import phase_5_housekeeping

        # Alternate between JSON and non-JSON responses
        responses = [
            '{"score": 4, "reason": "good"}',
            "SCORE: 3 - decent results",
            "The results are mediocre, I'd give them a 2",
        ]
        mock_llm_client.generate = AsyncMock(side_effect=responses)

        mock_search_results = [{"content": "test result"}]

        with patch.dict(
            "sys.modules",
            {
                "lifecycle": MagicMock(
                    run_lifecycle_maintenance=AsyncMock(
                        return_value={"facts_scored": 0, "decay_updated": 0}
                    ),
                ),
                "rag": MagicMock(
                    search_all_memory=MagicMock(return_value=mock_search_results),
                ),
            },
        ):
            results = await phase_5_housekeeping(mock_llm_client)

        quality = results.get("quality", {})
        # Should still produce a valid average
        assert quality.get("average_score", 0) > 0


# ═══════════════════════════════════════════════════════════════════
# Phase 6: Cleanup
# ═══════════════════════════════════════════════════════════════════


class TestPhase6Cleanup:
    """Tests for ingested_items cleanup."""

    def test_cleanup_calls_cleanup_old_items(self):
        """phase_6_cleanup should call ingest_state.cleanup_old_items."""
        from intelligence_pipeline import phase_6_cleanup

        with patch("ingest_state.psycopg2.connect") as mock_connect:
            mock_cur = MagicMock()
            mock_cur.rowcount = 5
            mock_connect.return_value.cursor.return_value = mock_cur
            mock_connect.return_value.close = MagicMock()
            mock_connect.return_value.commit = MagicMock()

            results = phase_6_cleanup()

        assert results["items_pruned"] == 5


# ═══════════════════════════════════════════════════════════════════
# Pipeline Structure
# ═══════════════════════════════════════════════════════════════════


class TestPipelineReportStructure:
    """Smoke tests for the pipeline report."""

    def test_report_has_expected_phase_keys(self):
        """The report dict should have all expected phase keys for Tier 3."""
        expected_keys = [
            "p1_catchup",
            "p2_relationships",
            "p3_engagement",
            "p4_patterns",
            "p5_housekeeping",
            "p6_cleanup",
        ]
        import inspect

        import intelligence_pipeline as ip

        source = inspect.getsource(ip.main)
        for key in expected_keys:
            assert key in source, f"Report should include phase key '{key}'"

    def test_no_ingestion_phases_in_main(self):
        """Tier 3 should NOT have ingestion phases (moved to Tier 1)."""
        import inspect

        import intelligence_pipeline as ip

        source = inspect.getsource(ip.main)
        # These ingestion phase keys should NOT be in the main function
        assert "p1_emails" not in source
        assert "p2_tasks" not in source
        assert "p3_contacts" not in source
        assert "p4_chatwoot" not in source
        assert "p5_crm" not in source
