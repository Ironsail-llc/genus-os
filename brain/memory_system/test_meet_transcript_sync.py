"""
Google Meet Transcript Sync — Test Suite

Unit tests (mocked gog CLI) for the sync script and parser.
Integration test for ingest_google_meet dedup.

Run unit tests:
    cd ~/robothor/brain/memory_system && ./venv/bin/python -m pytest test_meet_transcript_sync.py -v -m "not integration"

Run integration tests:
    cd ~/robothor/brain/memory_system && ./venv/bin/python -m pytest test_meet_transcript_sync.py -v -m integration
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make scripts importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))


# ═══════════════════════════════════════════════════════════════════
# Sample Gemini Notes text fixtures
# ═══════════════════════════════════════════════════════════════════

SAMPLE_FULL_DOC = """\
\ufeff📝 Notes
Feb 13, 2026
Sprint Planning
Invited Elguja Nemsadze Gregory Popov Philip D'Agostino Rochelle Blaza
Attachments Sprint Planning
Meeting records Recording
Meeting records Transcript Sprint Planning


Summary
The 'okay capture' workflow was successfully deployed to production. Separately, the script for creating accounts is being prepared.

Automation Review and Migration Strategy
A plan is needed to fix existing automation issues.


Details
Decisions
Rate these decisions: Helpful or Not Helpful


ALIGNED
Gregory Executes Practicing Account Script
Gregory executes production account creation script.


Define Medusa Automation Migration Scope
Nadjib, Philip define Medusa migration scope.


IVCure Bulk Drug Upload Via Backend
Backend bulk upload used for IVCure drugs.


More details:
* Production Deployment: Nadjib deployed the workflow to production.
* Data Migration: Gregory is focusing on patient migration.


Suggested next steps
* Nadjib will ask Conrad about the dosage issue.
* Gregory will prepare the script for patient migration.
* Rochelle will call Jhon Ray to discuss new fields.


You should review Gemini's notes to make sure they're accurate. Get tips and learn how Gemini takes notes
Please provide feedback about using Gemini to take notes in a short survey.
📖 Transcript
Feb 13, 2026
Sprint Planning - Transcript


Gregory Popov: Come on.
Jhon Ray Angcon: Good morning.
Gregory Popov: Oh,
Nadjib Boumekhiet: Hello.
Rochelle Blaza: Hey, good Okay. Um, all right. I think we can start. So, it's just Elguja is I think you'll join in shortly.
Gregory Popov: Let's wait for a bit.
Rochelle Blaza: Okay, so yeah. Let me share my screen. All right. So let's review our board.
"""

SAMPLE_MINIMAL_DOC = """\
\ufeff📝 Notes
Feb 9, 2026
Weekly Alignment Meetings
Invited Philip D'Agostino Craig Nicholson Robothor MHI
Attachments Weekly Alignment Meetings
Meeting records Transcript


Summary
A summary wasn't produced for this meeting because there wasn't enough conversation in a supported language.

If the meeting was transcribed, you can review the transcript linked in the meeting records section of this document.

Visit the help center for troubleshooting information


Details
Details weren't produced for this meeting.


Suggested next steps
No suggested next steps were found for this meeting.


You should review Gemini's notes to make sure they're accurate. Get tips and learn how Gemini takes notes
Please provide feedback about using Gemini to take notes in a short survey.
📖 Transcript
Feb 9, 2026
Weekly Alignment Meetings - Transcript
Transcription ended after 00:02:31


This editable transcript was computer generated and might contain errors.
"""


# ═══════════════════════════════════════════════════════════════════
# Parser Tests
# ═══════════════════════════════════════════════════════════════════


class TestParseGeminiNotes:
    def test_parses_full_structure(self):
        """Full doc: summary, decisions, next steps, transcript all extracted."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_FULL_DOC)

        assert result["summary"] != ""
        assert "okay capture" in result["summary"]
        assert len(result["decisions"]) == 3
        assert len(result["nextSteps"]) == 3
        assert "Gregory Popov: Come on." in result["transcript"]

    def test_extracts_attendees(self):
        """Invited line parsed into name list."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_FULL_DOC)

        assert len(result["attendees"]) >= 3
        assert any("Gregory Popov" in a for a in result["attendees"])
        assert any("Philip" in a for a in result["attendees"])

    def test_extracts_decisions(self):
        """ALIGNED blocks captured as decisions."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_FULL_DOC)

        assert any("Gregory" in d for d in result["decisions"])
        assert any("Medusa" in d for d in result["decisions"])

    def test_extracts_next_steps(self):
        """Suggested next steps parsed into list."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_FULL_DOC)

        assert len(result["nextSteps"]) == 3
        assert any("Nadjib" in s for s in result["nextSteps"])
        assert any("Gregory" in s for s in result["nextSteps"])

    def test_minimal_doc_no_crash(self):
        """Doc with no real content doesn't crash, returns empty sections."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_MINIMAL_DOC)

        assert result["summary"] == ""
        assert result["decisions"] == []
        assert result["nextSteps"] == []
        assert result["transcript"] == ""

    def test_minimal_doc_has_attendees(self):
        """Even minimal doc extracts attendees."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_MINIMAL_DOC)

        assert len(result["attendees"]) >= 2

    def test_transcript_starts_at_speaker(self):
        """Transcript section starts at the first speaker line."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes(SAMPLE_FULL_DOC)

        assert result["transcript"].startswith("Gregory Popov:")

    def test_strips_bom(self):
        """BOM character at start is handled."""
        from meet_transcript_sync import parse_gemini_notes

        result = parse_gemini_notes("\ufeffSome text")
        # Should not crash
        assert isinstance(result, dict)


class TestExtractMeetingTitle:
    def test_extracts_title(self):
        from meet_transcript_sync import extract_meeting_title

        assert (
            extract_meeting_title("Sprint Planning - 2026/02/13 09:55 EST - Notes by Gemini")
            == "Sprint Planning"
        )

    def test_leadership_meeting(self):
        from meet_transcript_sync import extract_meeting_title

        assert (
            extract_meeting_title(
                "Thrive Rx Weekly Leadership Meeting - 2026/02/06 11:28 EST - Notes by Gemini"
            )
            == "Thrive Rx Weekly Leadership Meeting"
        )

    def test_no_separator(self):
        from meet_transcript_sync import extract_meeting_title

        assert extract_meeting_title("Just a title") == "Just a title"


class TestExtractMeetingDate:
    def test_extracts_date(self):
        from meet_transcript_sync import extract_meeting_date

        assert (
            extract_meeting_date("Sprint Planning - 2026/02/13 09:55 EST - Notes by Gemini")
            == "2026-02-13"
        )

    def test_no_date(self):
        from meet_transcript_sync import extract_meeting_date

        assert extract_meeting_date("No date here") is None


# ═══════════════════════════════════════════════════════════════════
# Transcript Segmentation Tests
# ═══════════════════════════════════════════════════════════════════


class TestSegmentTranscript:
    def test_splits_by_speaker_boundary(self):
        """Segments respect speaker boundaries."""
        from continuous_ingest import _segment_transcript

        # Build a transcript longer than max_chars
        lines = []
        for i in range(100):
            speaker = "Alice" if i % 2 == 0 else "Bob"
            lines.append(f"{speaker}: {'x' * 40}")
        transcript = "\n".join(lines)

        segments = _segment_transcript(transcript, max_chars=500)
        assert len(segments) > 1

        # Each segment should be under max_chars (with some slack for not splitting mid-line)
        for seg in segments:
            assert len(seg) < 600  # allow some slack

    def test_short_transcript_single_segment(self):
        """Short transcript stays as one segment."""
        from continuous_ingest import _segment_transcript

        transcript = "Alice: Hello.\nBob: Hi there."
        segments = _segment_transcript(transcript, max_chars=3000)
        assert len(segments) == 1
        assert "Alice: Hello." in segments[0]

    def test_empty_transcript(self):
        """Empty transcript returns empty list."""
        from continuous_ingest import _segment_transcript

        segments = _segment_transcript("", max_chars=3000)
        assert segments == []


# ═══════════════════════════════════════════════════════════════════
# Sync Logic Tests (mocked gog CLI)
# ═══════════════════════════════════════════════════════════════════


class TestSyncLogic:
    def test_skips_unchanged_docs(self):
        """Docs with same modifiedTime are skipped."""
        from meet_transcript_sync import _run_sync

        doc_id = "test_doc_123"
        modified = "2026-02-13T18:57:14.937Z"

        fake_docs = [{"id": doc_id, "modifiedTime": modified, "name": "Test - Notes by Gemini"}]

        with (
            patch("meet_transcript_sync.fetch_gemini_docs", return_value=fake_docs),
            patch("meet_transcript_sync.load_json") as mock_load,
            patch("meet_transcript_sync.save_json") as mock_save,
            patch("meet_transcript_sync.download_doc_as_text") as mock_download,
        ):
            # State already has this doc at same modifiedTime
            mock_load.side_effect = lambda p: (
                {doc_id: modified} if "state" in str(p) else {"entries": {}}
            )

            _run_sync()

            # download should NOT have been called
            mock_download.assert_not_called()

    def test_downloads_new_docs(self):
        """New docs (not in state) are downloaded and parsed."""
        from meet_transcript_sync import _run_sync

        doc_id = "new_doc_456"
        modified = "2026-02-13T18:57:14.937Z"
        fake_docs = [
            {
                "id": doc_id,
                "modifiedTime": modified,
                "name": "Sprint Planning - 2026/02/13 09:55 EST - Notes by Gemini",
            }
        ]

        with (
            patch("meet_transcript_sync.fetch_gemini_docs", return_value=fake_docs),
            patch("meet_transcript_sync.load_json") as mock_load,
            patch("meet_transcript_sync.save_json") as mock_save,
            patch("meet_transcript_sync.download_doc_as_text", return_value=SAMPLE_FULL_DOC),
        ):
            # Empty state — no docs synced yet
            mock_load.side_effect = lambda p: {}

            _run_sync()

            # save_json should have been called with log containing the entry
            calls = mock_save.call_args_list
            # Find the call that saved the log (has "entries" key)
            log_saved = None
            for call in calls:
                data = call[0][0]
                if "entries" in data:
                    log_saved = data
                    break

            assert log_saved is not None
            assert doc_id in log_saved["entries"]
            entry = log_saved["entries"][doc_id]
            assert entry["title"] == "Sprint Planning"
            assert entry["date"] == "2026-02-13"
            assert len(entry["decisions"]) == 3

    def test_handles_download_failure(self):
        """Failed download is skipped gracefully."""
        from meet_transcript_sync import _run_sync

        fake_docs = [{"id": "fail_doc", "modifiedTime": "2026-01-01T00:00:00Z", "name": "Test"}]

        with (
            patch("meet_transcript_sync.fetch_gemini_docs", return_value=fake_docs),
            patch("meet_transcript_sync.load_json", return_value={}),
            patch("meet_transcript_sync.save_json") as mock_save,
            patch("meet_transcript_sync.download_doc_as_text", return_value=None),
        ):
            _run_sync()  # Should not raise


class TestCalendarMatching:
    def test_matches_by_title_and_date(self):
        """Matches calendar event by title substring + date."""
        from meet_transcript_sync import match_calendar_event

        cal_data = {
            "meetings": [
                {
                    "id": "cal_123",
                    "title": "Sprint Planning",
                    "start": "2026-02-13T10:00:00-05:00",
                },
                {
                    "id": "cal_456",
                    "title": "Daily Standup Meeting",
                    "start": "2026-02-13T09:55:00-05:00",
                },
            ]
        }

        with patch("meet_transcript_sync.load_json", return_value=cal_data):
            result = match_calendar_event("Sprint Planning", "2026-02-13")
            assert result == "cal_123"

    def test_no_match_wrong_date(self):
        """No match when date doesn't align."""
        from meet_transcript_sync import match_calendar_event

        cal_data = {
            "meetings": [
                {
                    "id": "cal_123",
                    "title": "Sprint Planning",
                    "start": "2026-02-12T10:00:00-05:00",
                },
            ]
        }

        with patch("meet_transcript_sync.load_json", return_value=cal_data):
            result = match_calendar_event("Sprint Planning", "2026-02-13")
            assert result is None

    def test_no_date_returns_none(self):
        """Null date returns None without searching."""
        from meet_transcript_sync import match_calendar_event

        assert match_calendar_event("Sprint Planning", None) is None


# ═══════════════════════════════════════════════════════════════════
# Integration: Ingest Dedup Test
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIngestGoogleMeetDedup:
    @pytest.fixture(autouse=True)
    def cleanup_dedup_state(self):
        """Remove stale ingested_items entry from prior runs."""
        import psycopg2

        try:
            conn = psycopg2.connect(dbname="robothor_memory")
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM ingested_items WHERE source_name = 'google_meet' AND item_id = 'test_dedup_doc'"
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        yield
        # Clean up after test too
        try:
            conn = psycopg2.connect(dbname="robothor_memory")
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM ingested_items WHERE source_name = 'google_meet' AND item_id = 'test_dedup_doc'"
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass

    def test_second_run_produces_zero_new(self):
        """Running ingestion twice on same data produces 0 new items second time."""
        import asyncio

        from continuous_ingest import ingest_google_meet

        sample_log = {
            "entries": {
                "test_dedup_doc": {
                    "docId": "test_dedup_doc",
                    "title": "Test Meeting",
                    "date": "2026-01-01",
                    "modifiedTime": "2026-01-01T12:00:00Z",
                    "attendees": ["alice@example.com"],
                    "summary": "Test summary for dedup.",
                    "decisions": [],
                    "nextSteps": [],
                    "transcript": "Alice: Hello.\nBob: Hi.",
                    "calendarEventId": None,
                    "syncedAt": "2026-01-01T12:00:00",
                }
            }
        }

        with patch("continuous_ingest.MEMORY_DIR") as mock_dir:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = json.dumps(sample_log)
            mock_dir.__truediv__ = MagicMock(return_value=mock_path)

            # Mock ingest_content to avoid real LLM calls (local import in function)
            with patch("ingestion.ingest_content", new_callable=AsyncMock) as mock_ingest:
                mock_ingest.return_value = {"fact_ids": [999]}

                # First run
                r1 = asyncio.get_event_loop().run_until_complete(ingest_google_meet())

                # Second run — same data, should be deduped
                r2 = asyncio.get_event_loop().run_until_complete(ingest_google_meet())

                # First run should ingest, second should skip
                assert r1["new"] >= 1
                assert r2["new"] == 0
