#!/usr/bin/env python3
"""Render docs/CRON_MAP.md from what is actually scheduled on this box.

CRON_MAP.md was hand-maintained, and by 2026-09 it documented six scripts that
no longer existed anywhere on disk. A schedule document nobody can regenerate
drifts, silently, in the direction of describing a machine that is gone — and
it drifts in the one direction that matters, because the entries that rot are
exactly the ones nobody has looked at recently.

Three sources, because the box runs three schedulers and no single one of them
is "the schedule":

  1. the operator's crontab (``crontab -l``),
  2. systemd timers (``systemctl list-timers --all --no-legend``),
  3. the engine's own ``agent_schedules`` rows.

Each is injectable, so this is testable with no crontab, no systemd and no
database. Every crontab target is checked against the filesystem and marked
MISSING when it is not there — the check the hand-maintained document could
never perform on itself.

Output goes to STDOUT. docs/CRON_MAP.md is gitignored instance data; the
redirect into it belongs to the caller on the box. A generator that writes the
file itself is one `cd` away from committing instance data.

Usage:
  gen_cron_map.py [--crontab-file F] [--timers-file F] [--workspace DIR] [--no-db]

Exit: 0 = every source was read, 1 = at least one source could not be read
(the document says so in-band too, but the caller redirecting into CRON_MAP.md
reads an exit code, not prose). `--no-db` is a deliberate skip, not a failure.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Extensions that make a crontab token a script whose existence can be checked.
#: Deliberately narrow: an interpreter (`venv/bin/python`) and a redirect target
#: (`logs/x.log`) are not schedule targets, and claiming a log file is MISSING
#: would train the reader to skim past the word.
SCRIPT_SUFFIXES = (".py", ".sh")

TIMER_PREFIXES = ("robothor-", "delphi-")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CD_PREFIX = re.compile(r"^cd\s+(\S+)\s*&&\s*")
_TOKEN = re.compile(r"[^\s;|&<>()\"']+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def default_workspace() -> Path:
    # `or`, not a default argument: a default is evaluated on every call, so
    # Path.home() ran even when ROBOTHOR_WORKSPACE said where the workspace is
    # — and under a unit with no HOME and no passwd entry for the service user
    # it raises. It also makes `ROBOTHOR_WORKSPACE=` in an env file mean the
    # current directory instead of "unset".
    workspace = os.environ.get("ROBOTHOR_WORKSPACE")
    return Path(workspace) if workspace else Path.home() / "robothor"


# ── crontab ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CronEntry:
    schedule: str
    command: str
    targets: tuple[str, ...]
    base_dir: str | None


def _targets(command: str) -> tuple[tuple[str, ...], str | None]:
    """Script paths named by a crontab command, plus the directory it cd's to.

    `cd X && ... y.py` runs y.py in X, not in the workspace. Resolving such a
    target against the workspace would report a live job as MISSING, and one
    false alarm is enough to make the whole column unread.
    """
    base_dir = None
    match = _CD_PREFIX.match(command)
    if match:
        base_dir = match.group(1)
    targets = []
    for token in _TOKEN.findall(command):
        if "$" in token:
            continue  # an unexpanded variable is not a claim we can check
        if token.endswith(SCRIPT_SUFFIXES):
            targets.append(token)
    return tuple(targets), base_dir


def parse_crontab(text: str) -> tuple[list[str], list[CronEntry]]:
    """(assignments, entries) from crontab text.

    Comment lines are dropped: a commented-out job is not a schedule, and
    documenting one as live is precisely how CRON_MAP came to describe scripts
    that no longer exist. Assignments (``W=``, ``MAILTO=``, ``PATH=``) are kept
    — they are not jobs, but every job line is meaningless without them.
    """
    assignments: list[str] = []
    entries: list[CronEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _ASSIGNMENT.match(line):
            assignments.append(line)
            continue
        if line.startswith("@"):
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            schedule, command = parts[0], parts[1]
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            schedule, command = " ".join(parts[:5]), parts[5]
        targets, base_dir = _targets(command)
        entries.append(
            CronEntry(schedule=schedule, command=command, targets=targets, base_dir=base_dir)
        )
    return assignments, entries


def missing_targets(entry: CronEntry, workspace: Path) -> list[str] | None:
    """Targets of this entry that are not on disk, or None when unresolvable.

    `cd $W && ./x.sh` cannot be checked: `$W` is a crontab assignment this
    generator does not expand, so the directory the job actually runs in is
    unknown. Resolving `./x.sh` against the workspace instead would invent a
    location, and print either a MISSING that is false or an `ok` that nothing
    verified. None says the check did not happen — the same distinction the
    schedule section already draws between "no rows" and "not read".
    """
    base = workspace
    if entry.base_dir:
        if "$" in entry.base_dir:
            return None
        candidate = Path(entry.base_dir)
        base = candidate if candidate.is_absolute() else workspace / candidate
    missing = []
    for target in entry.targets:
        path = Path(target)
        if not path.is_absolute():
            path = base / target
        if not path.exists():
            missing.append(target)
    return missing


# ── systemd timers ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimerRow:
    next_run: str
    left: str
    unit: str
    activates: str

    @property
    def sort_key(self) -> tuple[int, str]:
        # ISO timestamps sort correctly as plain strings, so no naive datetime
        # is constructed. A timer with no next run sorts last — it is stopped,
        # and burying the live ones under it would invert the document.
        if self.next_run == "-":
            return (1, self.unit)
        return (0, self.next_run)


def parse_timers(text: str, prefixes: tuple[str, ...] = TIMER_PREFIXES) -> list[TimerRow]:
    """Rows from `systemctl list-timers --all --no-legend`, sorted by next run.

    Columns are NEXT LEFT LAST PASSED UNIT ACTIVATES, where NEXT and LAST are
    four whitespace-separated tokens ("Tue 2026-09-02 16:00:00 EDT") or a bare
    "-", and PASSED can be two or three ("1 day ago"). Positional parsing of
    that is a bug waiting to happen, so the UNIT column is located by the
    `.timer` suffix and everything else is read relative to it.
    """
    rows: list[TimerRow] = []
    for line in text.splitlines():
        tokens = line.split()
        unit_index = next((i for i, t in enumerate(tokens) if t.endswith(".timer")), None)
        if unit_index is None:
            continue
        unit = tokens[unit_index]
        if not unit.startswith(prefixes):
            continue
        activates = tokens[unit_index + 1] if len(tokens) > unit_index + 1 else "-"
        head = tokens[:unit_index]
        if len(head) >= 4 and _ISO_DATE.match(head[1] if len(head) > 1 else ""):
            next_run = f"{head[1]} {head[2]}"
            left = " ".join(head[4:6]) if len(head) >= 6 else "-"
        else:
            next_run = "-"
            left = "-"
        rows.append(TimerRow(next_run=next_run, left=left, unit=unit, activates=activates))
    return sorted(rows, key=lambda r: r.sort_key)


# ── agent schedules ──────────────────────────────────────────────────────────


def fetch_schedules() -> list[dict[str, object]]:
    """The engine's own schedule rows. Read-only — this generator never writes."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, cron_expr, enabled, delivery_mode "
            "FROM agent_schedules ORDER BY agent_id"
        )
        return [
            {
                "agent_id": row[0],
                "cron_expr": row[1],
                "enabled": row[2],
                "delivery_mode": row[3],
            }
            for row in cur.fetchall()
        ]


# ── rendering ────────────────────────────────────────────────────────────────


def _cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "\\|")


def render(
    crontab_text: str | None,
    timers_text: str | None,
    schedules: list[dict[str, object]] | None,
    workspace: Path,
) -> str:
    """The whole document.

    `None` means the input was NOT READ, for every one of the three sources.
    "the read failed" and "there is nothing scheduled" are different facts, and
    only one of them is safe to print into a document the operator trusts to
    tell them what runs on this box.
    """
    assignments, entries = parse_crontab(crontab_text or "")
    lines: list[str] = [
        "# Cron Map",
        "",
        "Generated by `scripts/gen_cron_map.py` — do not hand-edit; regenerate.",
        "",
        "Three schedulers run on this box and no one of them is the schedule:",
        "the operator's crontab, systemd timers, and the engine's own",
        "`agent_schedules` rows. All three are below.",
        "",
        "## Crontab",
        "",
    ]
    if assignments:
        lines.append("Environment assignments (every job line below depends on these):")
        lines.append("")
        lines.extend(f"- `{a}`" for a in assignments)
        lines.append("")
    if entries:
        lines.append("| Schedule | Command | Target status |")
        lines.append("|---|---|---|")
        for entry in entries:
            missing = missing_targets(entry, workspace)
            if not entry.targets:
                status = "-"
            elif missing is None:
                status = "_not checked_ — the `cd` target is an unexpanded variable"
            elif missing:
                status = "**MISSING**: " + ", ".join(f"`{m}`" for m in missing)
            else:
                status = "ok"
            lines.append(f"| `{_cell(entry.schedule)}` | `{_cell(entry.command)}` | {status} |")
    elif crontab_text is None:
        lines.append(
            "_Not read_ — `crontab -l` failed, so the operator's crontab was "
            "not read. This is not the same as 'no cron jobs are scheduled'."
        )
    else:
        lines.append("No active crontab entries.")
    lines.append("")

    lines.append("## Systemd timers")
    lines.append("")
    rows = parse_timers(timers_text or "")
    if rows:
        lines.append("| Next run | Left | Timer | Activates |")
        lines.append("|---|---|---|---|")
        lines.extend(
            f"| {_cell(r.next_run)} | {_cell(r.left)} | `{r.unit}` | `{r.activates}` |"
            for r in rows
        )
    elif timers_text is None:
        lines.append(
            "_Not read_ — `systemctl list-timers` failed, so the timers were "
            "not read. This is not the same as 'there are no timers'."
        )
    else:
        lines.append("No `robothor-*` or `delphi-*` timers.")
    lines.append("")

    lines.append("## Agent schedules")
    lines.append("")
    if schedules is None:
        lines.append(
            "_Not read_ — this run was invoked with `--no-db`, so the "
            "`agent_schedules` rows were skipped. This is not the same as "
            "'no agents are scheduled'."
        )
    elif not schedules:
        lines.append("No rows in `agent_schedules`.")
    else:
        lines.append("| Agent | Cron | Enabled | Delivery |")
        lines.append("|---|---|---|---|")
        lines.extend(
            f"| `{_cell(s.get('agent_id'))}` | `{_cell(s.get('cron_expr'))}` "
            f"| {_cell(s.get('enabled'))} | {_cell(s.get('delivery_mode'))} |"
            for s in schedules
        )
    lines.append("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _read(source: str | None, command: list[str]) -> str | None:
    """The input's text, or None when it could not be read.

    Returning "" for a failed read would render as "No active crontab
    entries." — a positive fact about a source nobody managed to consult,
    printed into the document the operator trusts to say what runs here.
    """
    if source is not None:
        return Path(source).read_text()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"gen_cron_map: could not run {command[0]}: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"gen_cron_map: {' '.join(command)} exited {result.returncode}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crontab-file", help="read crontab text from this file instead of `crontab -l`"
    )
    parser.add_argument(
        "--timers-file", help="read `systemctl list-timers` text from this file instead of systemd"
    )
    parser.add_argument(
        "--workspace", help="resolve relative crontab targets against this directory"
    )
    parser.add_argument(
        "--no-db", action="store_true", help="skip the agent_schedules rows (no database needed)"
    )
    args = parser.parse_args(argv)

    crontab_text = _read(args.crontab_file, ["crontab", "-l"])
    timers_text = _read(args.timers_file, ["systemctl", "list-timers", "--all", "--no-legend"])

    schedules: list[dict[str, object]] | None = None
    db_failed = False
    if not args.no_db:
        try:
            schedules = fetch_schedules()
        except Exception as exc:
            # Loud and in-band: the section will say the rows were not read,
            # which is a different statement from "no agents are scheduled".
            print(f"gen_cron_map: agent_schedules unavailable: {exc}", file=sys.stderr)
            db_failed = True

    workspace = Path(args.workspace) if args.workspace else default_workspace()
    sys.stdout.write(render(crontab_text, timers_text, schedules, workspace))
    # The document says so in-band, but the caller redirecting this into
    # CRON_MAP.md reads an exit code, not prose. A partial map that exits 0 is
    # indistinguishable from a complete one.
    if crontab_text is None or timers_text is None or db_failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
