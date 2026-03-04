"""
Continuous Ingest (Tier 1) — Test Suite

Unit tests (mocked DB + LLM) and integration tests (real DB).

Run unit tests:
    cd ~/clawd/memory_system && ./venv/bin/python -m pytest test_continuous_ingest.py -v -m "not integration"

Run integration tests:
    cd ~/clawd/memory_system && ./venv/bin/python -m pytest test_continuous_ingest.py -v -m integration
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ingest_state import content_hash

# ═══════════════════════════════════════════════════════════════════
# Unit Tests — content hashing
# ═══════════════════════════════════════════════════════════════════


class TestContentHash:
    def test_deterministic(self):
        """Same input → same hash."""
        data = {"from": "alice@example.com", "subject": "Hello", "urgency": "medium"}
        h1 = content_hash(data, ["from", "subject", "urgency"])
        h2 = content_hash(data, ["from", "subject", "urgency"])
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_data_different_hash(self):
        """Different input → different hash."""
        d1 = {"from": "alice@example.com", "subject": "Hello"}
        d2 = {"from": "alice@example.com", "subject": "Goodbye"}
        h1 = content_hash(d1, ["from", "subject"])
        h2 = content_hash(d2, ["from", "subject"])
        assert h1 != h2

    def test_key_order_irrelevant(self):
        """Keys are sorted internally, so order doesn't matter."""
        data = {"b": "2", "a": "1"}
        h1 = content_hash(data, ["a", "b"])
        h2 = content_hash(data, ["b", "a"])
        assert h1 == h2

    def test_missing_keys_default_to_empty(self):
        """Missing keys hash as empty string."""
        data = {"a": "1"}
        h = content_hash(data, ["a", "nonexistent"])
        assert len(h) == 64

    def test_none_values_treated_as_empty(self):
        """None values are treated the same as empty string."""
        d1 = {"a": None}
        d2 = {"a": ""}
        h1 = content_hash(d1, ["a"])
        h2 = content_hash(d2, ["a"])
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════════
# Unit Tests — ingest_state module
# ═══════════════════════════════════════════════════════════════════


class TestIngestState:
    @patch("ingest_state.psycopg2.connect")
    def test_is_already_ingested_new_item(self, mock_connect):
        """New item (not in DB) returns False."""
        from ingest_state import is_already_ingested

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_connect.return_value)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value.cursor.return_value = mock_cur
        # Override close to be a noop since we're mocking
        mock_connect.return_value.close = MagicMock()

        result = is_already_ingested("email", "abc123", "hashval")
        assert result is False

    @patch("ingest_state.psycopg2.connect")
    def test_is_already_ingested_same_hash(self, mock_connect):
        """Existing item with same hash returns True (skip)."""
        from ingest_state import is_already_ingested

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("hashval",)
        mock_connect.return_value.cursor.return_value = mock_cur
        mock_connect.return_value.close = MagicMock()

        result = is_already_ingested("email", "abc123", "hashval")
        assert result is True

    @patch("ingest_state.psycopg2.connect")
    def test_is_already_ingested_different_hash(self, mock_connect):
        """Existing item with different hash returns False (re-ingest)."""
        from ingest_state import is_already_ingested

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("oldhash",)
        mock_connect.return_value.cursor.return_value = mock_cur
        mock_connect.return_value.close = MagicMock()

        result = is_already_ingested("email", "abc123", "newhash")
        assert result is False


# ═══════════════════════════════════════════════════════════════════
# Unit Tests — continuous_ingest sources
# ═══════════════════════════════════════════════════════════════════


class TestEmailIngestion:
    @pytest.mark.asyncio
    async def test_skips_uncategorized_emails(self):
        """Emails without categorizedAt are skipped."""
        mock_ingest = AsyncMock(return_value={"fact_ids": [1]})

        email_log = {
            "entries": {
                "email1": {
                    "from": "alice@example.com",
                    "subject": "Test",
                    # No categorizedAt — should be skipped
                },
            }
        }

        with (
            patch("continuous_ingest.MEMORY_DIR") as mock_dir,
            patch("ingestion.ingest_content", mock_ingest),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = json.dumps(email_log)
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            from continuous_ingest import ingest_emails

            results = await ingest_emails()

        mock_ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_noreply_low_urgency(self):
        """Low urgency noreply emails are skipped."""
        mock_ingest = AsyncMock(return_value={"fact_ids": [1]})

        email_log = {
            "entries": {
                "email1": {
                    "from": "noreply@github.com",
                    "subject": "New commit",
                    "categorizedAt": datetime.now().isoformat(),
                    "urgency": "low",
                    "summary": "CI passed",
                },
            }
        }

        with (
            patch("continuous_ingest.MEMORY_DIR") as mock_dir,
            patch("ingestion.ingest_content", mock_ingest),
            patch("ingest_state.psycopg2.connect") as mock_connect,
        ):
            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = None
            mock_connect.return_value.cursor.return_value = mock_cur
            mock_connect.return_value.close = MagicMock()

            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = json.dumps(email_log)
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            from continuous_ingest import ingest_emails

            results = await ingest_emails()

        mock_ingest.assert_not_called()
        assert results["skipped"] >= 1


class TestSourceIndependence:
    @pytest.mark.asyncio
    async def test_conversation_failure_doesnt_block_email(self):
        """If conversation ingestion fails, email ingestion still runs."""
        from continuous_ingest import run_continuous_ingest

        with (
            patch("continuous_ingest.ingest_emails", new_callable=AsyncMock) as mock_email,
            patch("continuous_ingest.ingest_calendar", new_callable=AsyncMock) as mock_cal,
            patch("continuous_ingest.ingest_tasks", new_callable=AsyncMock) as mock_tasks,
            patch("continuous_ingest.ingest_jira", new_callable=AsyncMock) as mock_jira,
            patch("continuous_ingest.ingest_conversations", new_callable=AsyncMock) as mock_conv,
            patch("continuous_ingest.ingest_twenty_crm", new_callable=AsyncMock) as mock_twenty,
            patch("continuous_ingest.ingest_contacts", new_callable=AsyncMock) as mock_contacts,
            patch("continuous_ingest.ingest_google_meet", new_callable=AsyncMock) as mock_meet,
        ):
            mock_email.return_value = {"new": 2, "skipped": 0, "errors": 0}
            mock_cal.return_value = {"new": 0, "skipped": 0, "errors": 0}
            mock_tasks.return_value = {"new": 1, "skipped": 0, "errors": 0}
            mock_jira.return_value = {"new": 0, "skipped": 0, "errors": 0}
            mock_conv.side_effect = Exception("Connection refused")
            mock_twenty.return_value = {"new": 0, "skipped": 0, "errors": 0}
            mock_contacts.return_value = {"new": 0, "skipped": 0, "errors": 0}
            mock_meet.return_value = {"new": 0, "skipped": 0, "errors": 0}

            results = await run_continuous_ingest()

            assert results["email"]["new"] == 2
            assert results["tasks"]["new"] == 1
            assert results["conversation"]["errors"] == 1


class TestLocking:
    @pytest.mark.asyncio
    async def test_skips_when_nightly_running(self):
        """Continuous ingest skips when nightly lock is held."""
        from continuous_ingest import is_nightly_running

        with patch("continuous_ingest.NIGHTLY_LOCK") as mock_lock:
            mock_lock.exists.return_value = False
            assert is_nightly_running() is False


class TestErrorRecording:
    @patch("ingest_state.psycopg2.connect")
    def test_error_recording_increments(self, mock_connect):
        """record_error increments error_count."""
        from ingest_state import record_error

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (3,)
        mock_connect.return_value.cursor.return_value = mock_cur
        mock_connect.return_value.close = MagicMock()
        mock_connect.return_value.commit = MagicMock()

        count = record_error("email", "Connection timeout")
        assert count == 3


# ═══════════════════════════════════════════════════════════════════
# Integration Tests — real DB
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestWatermarkPersistence:
    def test_watermark_roundtrip(self, db_conn):
        """Write watermark, read it back."""
        from ingest_state import get_watermark, update_watermark

        update_watermark("__test_source__", 5)
        wm = get_watermark("__test_source__")

        assert wm is not None
        assert wm["items_ingested"] == 5
        assert wm["error_count"] == 0

        # Cleanup
        cur = db_conn.cursor()
        cur.execute("DELETE FROM ingestion_watermarks WHERE source_name = '__test_source__'")
        db_conn.commit()

    def test_watermark_accumulates(self, db_conn):
        """Multiple updates accumulate items_ingested."""
        from ingest_state import get_watermark, update_watermark

        update_watermark("__test_accum__", 3)
        update_watermark("__test_accum__", 2)
        wm = get_watermark("__test_accum__")

        assert wm["items_ingested"] == 5

        # Cleanup
        cur = db_conn.cursor()
        cur.execute("DELETE FROM ingestion_watermarks WHERE source_name = '__test_accum__'")
        db_conn.commit()


@pytest.mark.integration
class TestIngestedItemsDedup:
    def test_dedup_roundtrip(self, db_conn):
        """Record an item, verify is_already_ingested returns True for same hash."""
        from ingest_state import is_already_ingested, record_ingested

        record_ingested("__test_src__", "__test_item_1__", "abc123hash", [1, 2])

        assert is_already_ingested("__test_src__", "__test_item_1__", "abc123hash") is True
        assert is_already_ingested("__test_src__", "__test_item_1__", "differenthash") is False
        assert is_already_ingested("__test_src__", "__test_item_2__", "abc123hash") is False

        # Cleanup
        cur = db_conn.cursor()
        cur.execute("DELETE FROM ingested_items WHERE source_name = '__test_src__'")
        db_conn.commit()

    def test_upsert_updates_hash(self, db_conn):
        """Recording same item with different hash updates the record."""
        from ingest_state import is_already_ingested, record_ingested

        record_ingested("__test_upsert__", "__test_item__", "hash_v1", [1])
        assert is_already_ingested("__test_upsert__", "__test_item__", "hash_v1") is True

        record_ingested("__test_upsert__", "__test_item__", "hash_v2", [1, 2])
        assert is_already_ingested("__test_upsert__", "__test_item__", "hash_v2") is True
        assert is_already_ingested("__test_upsert__", "__test_item__", "hash_v1") is False

        # Cleanup
        cur = db_conn.cursor()
        cur.execute("DELETE FROM ingested_items WHERE source_name = '__test_upsert__'")
        db_conn.commit()


@pytest.mark.integration
class TestErrorState:
    def test_error_then_success_resets(self, db_conn):
        """Successful watermark update resets error state."""
        from ingest_state import get_watermark, record_error, update_watermark

        record_error("__test_err__", "timeout")
        record_error("__test_err__", "timeout again")

        wm = get_watermark("__test_err__")
        assert wm["error_count"] == 2
        assert wm["last_error"] == "timeout again"

        # Success resets errors
        update_watermark("__test_err__", 1)
        wm = get_watermark("__test_err__")
        assert wm["error_count"] == 0
        assert wm["last_error"] is None

        # Cleanup
        cur = db_conn.cursor()
        cur.execute("DELETE FROM ingestion_watermarks WHERE source_name = '__test_err__'")
        db_conn.commit()
