#!/usr/bin/env python3
"""Ingest the weekly HR Claude Usage Report (PDF) for the devops report.

Default: fetch the newest email within 10 days whose sender is in
``DEVOPS_USAGE_REPORT_SENDERS`` (comma-separated) and whose subject contains
"Claude Usage Report", take its PDF attachment (gws CLI), convert it with
``pdftotext -layout`` and parse the usage table.

  --message-id ID   ingest one specific email (backfill)
  --pdf PATH        parse a local PDF instead of fetching

Writes ``$DEVOPS_RUNS_DIR/<week-start>/usage.json`` and archives the PDF and
parsed JSON as ``<report-date>.{pdf,json}`` under ``--archive-dir`` (default
``$ROBOTHOR_WORKSPACE/local/devops/claude_usage``). When the report cannot be
fetched or parsed, usage.json says ``available: false`` with the reason and
the script exits 1. Parsing: robothor/engine/reports/claude_usage_hr.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.claude_usage_hr import (
    extract_usage,
    fetch_latest_report,
    pdf_to_text,
)
from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_team import env_list, runs_dir, week_run_dir, workspace


def _senders() -> list[str]:
    return env_list("DEVOPS_USAGE_REPORT_SENDERS")


def _ingest(args: argparse.Namespace) -> tuple[dict, bytes | None]:
    if args.pdf:
        pdf_bytes = Path(args.pdf).read_bytes()
        source = {"pdf": str(args.pdf)}
        text = pdf_to_text(Path(args.pdf))
    else:
        got = fetch_latest_report(_senders(), message_id=args.message_id)
        pdf_bytes = got["pdf"]
        source = {k: got[k] for k in ("message_id", "subject", "sender", "internal_date")}
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            text = pdf_to_text(Path(tmp.name))
    usage = extract_usage(text)
    usage["source"] = source
    return usage, pdf_bytes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    ap.add_argument(
        "--archive-dir", type=Path, help="default $ROBOTHOR_WORKSPACE/local/devops/claude_usage"
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--message-id", help="ingest this Gmail message id")
    src.add_argument("--pdf", type=Path, help="parse this local PDF")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    out_path = week_run_dir(period["week_start"], args.runs_dir or runs_dir()) / "usage.json"
    archive = args.archive_dir or workspace() / "local" / "devops" / "claude_usage"

    pdf_bytes = None
    try:
        usage, pdf_bytes = _ingest(args)
    except Exception as e:  # noqa: BLE001 - every failure becomes an honest "unavailable"
        usage = {"available": False, "reason": f"{type(e).__name__}: {e}"}

    usage["collected_at"] = datetime.now(UTC).isoformat()
    usage["report_week"] = period["week_start"]
    out_path.write_text(json.dumps(usage, indent=2, ensure_ascii=False))

    if usage.get("available") and usage.get("report_date"):
        archive.mkdir(parents=True, exist_ok=True)
        stem = archive / usage["report_date"]
        stem.with_suffix(".json").write_text(json.dumps(usage, indent=2, ensure_ascii=False))
        if pdf_bytes:
            stem.with_suffix(".pdf").write_bytes(pdf_bytes)

    if usage.get("available"):
        print(
            f"{len(usage['rows'])} usage rows ({usage['method']}), report "
            f"{usage.get('report_date')} -> {out_path}"
        )
        return 0
    print(f"usage unavailable: {usage['reason']} -> {out_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
