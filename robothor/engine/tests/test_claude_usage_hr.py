"""Tests for the HR Claude-usage report ingest (PDF text -> validated rows).

Fixtures are generic re-creations of the two table shapes the weekly HR PDF
has used (``pdftotext -layout`` output), with fabricated names:

* ``usage_layout_session_note.txt`` — no Fable column; the Fable status sits in
  the note ("Fable Limit Reached (Fable 100%)").
* ``usage_layout_fable_column.txt`` — a Fable column between Current Session
  and Weekly Reset, a "Captured:" header line, differently wrapped accounts.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from robothor.engine.reports.claude_usage_hr import (
    extract_usage,
    fetch_latest_report,
    parse_usage_text,
)

FIX = Path(__file__).parent / "fixtures" / "claude_usage"
NOTE_SHAPE = (FIX / "usage_layout_session_note.txt").read_text()
FABLE_SHAPE = (FIX / "usage_layout_fable_column.txt").read_text()


def _by(rows, dev, acct):
    return next(r for r in rows if r["developer"] == dev and r["account"] == acct)


class TestNoteShape:
    def setup_method(self):
        self.out = parse_usage_text(NOTE_SHAPE)
        self.rows = self.out["rows"]

    def test_consistent_table(self):
        assert self.out["problems"] == []
        assert len(self.rows) == 9
        assert self.out["report_date"] == "2026-10-23"

    def test_wrapped_cells_are_joined(self):
        r = _by(self.rows, "Fatima", "Acct 1")  # "Fatima – Acct\n1"
        assert r["plan"] == "Max 5x"
        assert r["reset"] == "Sun 10:00 PM"
        assert r["weekly_pct"] == 79
        assert r["session_pct"] == 18
        g = _by(self.rows, "Gina Lee", "Acct 1")  # "Gina Lee –\nAcct 1"
        assert g["weekly_pct"] == 60

    def test_in_progress_is_mid_cycle_and_projected(self):
        r = _by(self.rows, "Fatima", "Acct 1")
        assert r["reading"] == "mid_cycle"
        assert r["limit_status"] == "projected"

    def test_post_reset(self):
        r = _by(self.rows, "Harriet", "Acct 1")  # "4% — POST-\nRESET"
        assert r["weekly_pct"] == 4
        assert r["reading"] == "post_reset"

    def test_free_reset_reading_and_prior_limit_hit(self):
        r = _by(self.rows, "Alice", "Acct 2")
        assert r["weekly_pct"] == 64
        assert r["reading"] == "post_reset"
        assert r["limit_status"] == "hit"

    def test_unverified(self):
        assert _by(self.rows, "Dana", "Acct 2")["reading"] == "unverified"

    def test_near_limit(self):
        r = _by(self.rows, "Alice", "Acct 1")
        assert r["limit_status"] == "near"
        assert r["reading"] == "completed"

    def test_fable_limit_from_note_is_not_a_weekly_limit(self):
        r = _by(self.rows, "Dana", "Acct 1")
        assert r["fable_limit_hit"] is True
        assert r["fable_pct"] == 100
        assert r["limit_status"] == "ok"
        assert _by(self.rows, "Carlos", "Acct 1")["fable_limit_hit"] is False

    def test_narrative_percentages_are_not_rows(self):
        assert {r["developer"] for r in self.rows} == {
            "Alice",
            "Dana",
            "Carlos",
            "Edward",
            "Fatima",
            "Gina Lee",
            "Harriet",
        }


class TestFableColumnShape:
    def setup_method(self):
        self.out = parse_usage_text(FABLE_SHAPE)
        self.rows = self.out["rows"]

    def test_consistent_table(self):
        assert self.out["problems"] == []
        assert len(self.rows) == 9
        assert self.out["report_date"] == "2026-10-15"

    def test_limit_hit_marker(self):
        r = _by(self.rows, "Alice", "Acct 1")  # "🔴 100% —\nLIMIT HIT"
        assert r["weekly_pct"] == 100
        assert r["limit_status"] == "hit"
        assert r["fable_pct"] == 4
        assert r["fable_limit_hit"] is False

    def test_fable_column_limit_reached(self):
        r = _by(self.rows, "Dana", "Acct 2")  # "100% — Fable\nlimit reached"
        assert r["fable_pct"] == 100
        assert r["fable_limit_hit"] is True
        assert r["weekly_pct"] == 54
        assert r["limit_status"] == "ok"

    def test_in_progress_upper_case(self):
        r = _by(self.rows, "Fatima", "Acct 1")  # "⚠️ 68% — IN\nPROGRESS"
        assert r["weekly_pct"] == 68
        assert r["reading"] == "mid_cycle"
        assert r["limit_status"] == "projected"

    def test_post_reset_and_wrapped_reset_time(self):
        assert _by(self.rows, "Carlos", "Acct 1")["reading"] == "post_reset"
        assert _by(self.rows, "Dana", "Acct 1")["reset"] == "Wed 10:00 AM"
        assert _by(self.rows, "Gina Lee", "Acct 1")["reading"] == "completed"


class TestValidation:
    def test_bad_pct_is_rejected(self):
        bad = NOTE_SHAPE.replace("72%            2%", "172%           2%")
        out = parse_usage_text(bad)
        assert out["problems"]

    def test_missing_pct_is_rejected(self):
        bad = NOTE_SHAPE.replace("28%            4%", "n/a            4%")
        assert parse_usage_text(bad)["problems"]

    def test_inconsistent_without_llm_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("DEVOPS_USAGE_LLM_MODEL", raising=False)
        bad = NOTE_SHAPE.replace("72%            2%", "172%           2%")
        out = extract_usage(bad)
        assert out["available"] is False
        assert "172" in out["reason"] or "pct" in out["reason"]

    def test_llm_fallback_output_is_validated_in_code(self):
        bad = NOTE_SHAPE.replace("72%            2%", "172%           2%")

        def fake_llm(text):
            return [
                {
                    "developer": "Alice",
                    "account": "Acct 1",
                    "plan": "Max 20x",
                    "weekly": "⚠️ 79% — In progress",
                    "session": "18%",
                    "fable": "",
                    "reset": "Sun 10:00 PM",
                    "note": "Projected Early Exhaustion",
                }
            ]

        out = extract_usage(bad, llm=fake_llm)
        assert out["available"] is True
        assert out["method"] == "llm"
        assert out["rows"][0]["reading"] == "mid_cycle"
        assert out["rows"][0]["limit_status"] == "projected"

    def test_llm_garbage_is_rejected(self):
        bad = NOTE_SHAPE.replace("72%            2%", "172%           2%")
        out = extract_usage(bad, llm=lambda t: [{"developer": "Alice", "weekly": "250%"}])
        assert out["available"] is False

    def test_parser_success_does_not_call_llm(self):
        def boom(text):
            raise AssertionError("LLM must not be called")

        out = extract_usage(NOTE_SHAPE, llm=boom)
        assert out["method"] == "parser"
        assert len(out["rows"]) == 9


class TestFetch:
    def _fake_gws(self, pdf: bytes, *, messages=None):
        calls = []

        def run(args, timeout=30):
            calls.append(args)
            params = json.loads(args[args.index("--params") + 1])
            if args[:4] == ["gmail", "users", "messages", "list"]:
                return {"messages": messages if messages is not None else [{"id": "m1"}]}
            if args[:4] == ["gmail", "users", "messages", "get"]:
                return {
                    "id": params["id"],
                    "internalDate": "1792800000000",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Weekly Claude Usage Report"},
                            {"name": "From", "value": "Payroll <hr@example.com>"},
                        ],
                        "parts": [
                            {"filename": "", "mimeType": "text/plain", "body": {}},
                            {
                                "filename": "usage.pdf",
                                "mimeType": "application/pdf",
                                "body": {"attachmentId": "att1"},
                            },
                        ],
                    },
                }
            if args[:5] == ["gmail", "users", "messages", "attachments", "get"]:
                assert params["messageId"] == "m1" and params["id"] == "att1"
                return {"data": base64.urlsafe_b64encode(pdf).decode().rstrip("=")}
            raise AssertionError(args)

        return run, calls

    def test_fetches_newest_pdf_from_configured_senders(self):
        run, calls = self._fake_gws(b"%PDF-1.4 fake")
        got = fetch_latest_report(["hr@example.com", "ops@example.com"], run=run)
        assert got["pdf"] == b"%PDF-1.4 fake"
        assert got["message_id"] == "m1"
        q = json.loads(calls[0][calls[0].index("--params") + 1])["q"]
        assert "from:hr@example.com" in q and "from:ops@example.com" in q
        assert "Claude Usage Report" in q
        assert "newer_than:10d" in q

    def test_specific_message_id_skips_search(self):
        run, calls = self._fake_gws(b"%PDF")
        got = fetch_latest_report([], run=run, message_id="m1")
        assert got["message_id"] == "m1"
        assert all(c[3] != "list" for c in calls)

    def test_no_message_raises(self):
        run, _ = self._fake_gws(b"", messages=[])
        with pytest.raises(LookupError):
            fetch_latest_report(["hr@example.com"], run=run)
