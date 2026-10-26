#!/usr/bin/env python3
"""Render the devops weekly report for one week's run directory.

Reads from ``$DEVOPS_RUNS_DIR/<week-start>/``:

  people.json   — the deterministic aggregate (required; every number comes from it)
  analyst.json  — the analyst's narrative (optional; checked by the narrative guard)
  jira.json     — for the open-backlog figure (optional)

Writes ``report.html`` (email body), ``report.json`` (the rendered context,
read by the Telegram step) and ``meta.json`` ({subject, plain_summary,
period}) next to them. A missing or contradictory narrative never blocks the
report: the guard withholds the note and the report says so.

Exit 1 only when people.json is missing or rendering fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_report import build_report_context
from robothor.engine.reports.devops_team import runs_dir, week_run_dir
from robothor.engine.reports.renderer import render_devops_report


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ignoring unreadable {path.name}: {e}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    run = week_run_dir(period["week_start"], args.runs_dir or runs_dir())
    for name in ("report.html", "report.json", "meta.json"):
        (run / name).unlink(missing_ok=True)

    people = _read(run / "people.json")
    if people is None:
        print(
            f"missing {run / 'people.json'} — run devops_aggregate_people.py first", file=sys.stderr
        )
        return 1
    analyst = _read(run / "analyst.json")
    ctx = build_report_context(people, analyst, jira=_read(run / "jira.json"))

    rendered = render_devops_report(ctx)
    if "error" in rendered:
        print(f"render failed: {rendered['error']}", file=sys.stderr)
        return 1

    (run / "report.html").write_text(rendered["html"])
    (run / "report.json").write_text(json.dumps(ctx, indent=2, default=str, ensure_ascii=False))
    (run / "meta.json").write_text(
        json.dumps(
            {
                "subject": rendered["subject"],
                "plain_summary": rendered["plain_summary"],
                "period": ctx.get("period"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    withheld = ctx["data_quality"].get("narrative_withheld") or []
    print(
        f"Rendered {len(rendered['html'])} chars -> {run / 'report.html'} "
        f"(subject: {rendered['subject']}; narrative withheld for {len(withheld)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
