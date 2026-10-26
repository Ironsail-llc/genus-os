"""scripts/devops_claude_usage.py — local PDF / text ingest, archive, run file."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_claude_usage.py"
_FIX = (
    Path(__file__).resolve().parents[2]
    / "robothor"
    / "engine"
    / "tests"
    / "fixtures"
    / "claude_usage"
    / "usage_layout_fable_column.txt"
)


def _load():
    spec = importlib.util.spec_from_file_location("devops_claude_usage_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_claude_usage_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_local_pdf_is_parsed_archived_and_written(tmp_path, monkeypatch):
    mod = _load()
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(mod, "pdf_to_text", lambda p: _FIX.read_text())
    archive = tmp_path / "archive"
    rc = mod.main(
        [
            "--pdf",
            str(pdf),
            "--week-start",
            "2026-10-12",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--archive-dir",
            str(archive),
        ]
    )
    assert rc == 0
    usage = json.loads((tmp_path / "runs" / "2026-10-12" / "usage.json").read_text())
    assert usage["available"] is True
    assert len(usage["rows"]) == 9
    assert (archive / "2026-10-15.json").exists()
    assert (archive / "2026-10-15.pdf").read_bytes() == b"%PDF-fake"


def test_no_email_writes_unavailable_and_exits_1(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DEVOPS_USAGE_REPORT_SENDERS", "hr@example.com")

    def none(*a, **k):
        raise LookupError("no 'Claude Usage Report' email with a PDF in the last 10 days")

    monkeypatch.setattr(mod, "fetch_latest_report", none)
    rc = mod.main(["--week-start", "2026-10-12", "--runs-dir", str(tmp_path)])
    assert rc == 1
    usage = json.loads((tmp_path / "2026-10-12" / "usage.json").read_text())
    assert usage["available"] is False
    assert "10 days" in usage["reason"]
