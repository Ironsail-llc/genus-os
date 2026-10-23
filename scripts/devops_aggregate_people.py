#!/usr/bin/env python3
"""Build the deterministic people table for one devops report week.

Reads ``github.json``, ``jira.json`` and ``usage.json`` from
``$DEVOPS_RUNS_DIR/<week-start>/`` (any may be missing: the metrics it feeds
become "unavailable", never zero), the team roster from ``$DEVOPS_TEAM_FILE``,
and the three previous weeks' ``people.json`` for trends. Writes
``people.json`` next to them, and points ``$DEVOPS_RUNS_DIR/latest`` at that
week so the analyst agent reads and writes fixed paths.

Rows exist only for active roster members. Exits 1 when the team file is
missing or invalid — without a roster there is no honest table to build.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.devops_aggregate import aggregate_people
from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_team import TeamFileError, load_team, runs_dir, week_run_dir


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"unreadable {path}: {e}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _point_latest(base: Path, run: Path) -> None:
    """``<runs>/latest`` -> this week, so the analyst agent has a fixed path."""
    link = base / "latest"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(run.name)
    except OSError as e:
        print(f"could not update {link}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    ap.add_argument("--team-file", type=Path, help="override $DEVOPS_TEAM_FILE")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    base = args.runs_dir or runs_dir()
    run = week_run_dir(period["week_start"], base)

    try:
        team = load_team(args.team_file)
    except TeamFileError as e:
        print(f"team file error: {e}", file=sys.stderr)
        return 1

    github = _read(run / "github.json")
    jira = _read(run / "jira.json")
    usage = _read(run / "usage.json")

    monday = date.fromisoformat(period["week_start"])
    history = [
        _read(base / (monday - timedelta(days=7 * k)).strftime("%Y-%m-%d") / "people.json")
        for k in (3, 2, 1)
    ]

    people = aggregate_people(team, github, jira, usage, history=history)
    people["period"] = period
    out = run / "people.json"
    out.write_text(json.dumps(people, indent=2, default=str))
    # A narrative written against an earlier table no longer describes this
    # one; the analyst step writes a fresh one (or the report says it is missing).
    (run / "analyst.json").unlink(missing_ok=True)
    _point_latest(base, run)

    dq = people["data_quality"]
    print(
        f"{len(people['members'])} roster rows, {len(dq['outside_roster'])} outside-roster "
        f"handles, {len(dq['no_activity'])} members with no activity, "
        f"{len(dq['unavailable_metrics'])} unavailable metrics -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
