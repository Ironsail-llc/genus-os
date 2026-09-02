"""Tests for scripts/gen_cron_map.py — the CRON_MAP generator.

docs/CRON_MAP.md was hand-maintained, and by 2026-09 it documented six
scripts that no longer existed anywhere on disk. A schedule document nobody
can regenerate is a schedule document that drifts, silently, in the direction
of describing a machine that is gone.

Three sources, because the box runs three schedulers and no single one of
them is the schedule: the operator's crontab, systemd timers, and the
engine's own ``agent_schedules`` rows. Each is injectable, so the generator
is testable without a crontab, without systemd and without a database.

The generator writes to STDOUT only. docs/CRON_MAP.md is gitignored instance
data; the controller redirects into it on the box. A generator that writes
the file itself is one `cd` away from committing instance data.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN = REPO_ROOT / "scripts" / "gen_cron_map.py"

_spec = importlib.util.spec_from_file_location("gen_cron_map", GEN)
assert _spec is not None and _spec.loader is not None
gcm = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules[cls.__module__]; a
# spec-loaded module must be registered there before it is executed.
sys.modules["gen_cron_map"] = gcm
_spec.loader.exec_module(gcm)


CRONTAB_TEMPLATE = """\
# Cron wrapper (W) sources the SOPS-decrypted secrets
W={ws}/scripts/cron-wrapper.sh

# Health sync — every 15 min, active hours only
*/15 6-22 * * * cd {ws} && $W venv/bin/python -m robothor.health.sync >> logs/health.log 2>&1

# MIGRATED to the engine — this line is OFF and must not be documented as live
# 0 3 * * * $W scripts/retired_job.sh

0 4 * * * $W scripts/present.sh >> logs/present.log 2>&1
30 5 * * * $W scripts/gone.sh
15 2 * * * cd {ws}/sub && $W ./venv/bin/python nested.py
"""

#: For the pure parsing tests, where no path is ever resolved.
CRONTAB = CRONTAB_TEMPLATE.format(ws="/srv/genus")


def crontab_for(ws: Path) -> str:
    """The same crontab, with its absolute paths pointing into the test
    workspace — real crontab lines `cd` to absolute directories."""
    return CRONTAB_TEMPLATE.format(ws=ws)


# `systemctl list-timers --all --no-legend`: NEXT LEFT LAST PASSED UNIT ACTIVATES,
# where NEXT and LAST are four tokens or a bare "-".
TIMERS = """\
Tue 2026-09-02 16:00:00 EDT 13min left Tue 2026-09-02 15:00:00 EDT 46min ago robothor-liveness.timer robothor-liveness.service
Tue 2026-09-02 04:00:00 EDT 12h left - - robothor-backup-local.timer robothor-backup-local.service
- - Mon 2026-09-01 03:00:00 EDT 1 day ago delphi-report.timer delphi-report.service
Tue 2026-09-02 05:00:00 EDT 13h left - - apt-daily.timer apt-daily.service
"""

SCHEDULES = [
    {"agent_id": "main", "cron_expr": "0 * * * *", "enabled": True, "delivery_mode": "telegram"},
    {"agent_id": "crm-hygiene", "cron_expr": "0 3 * * *", "enabled": False, "delivery_mode": None},
]


def workspace(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "present.sh").write_text("#!/bin/sh\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("")
    return tmp_path


# ── crontab ──────────────────────────────────────────────────────────────────


def test_commented_crontab_line_is_excluded(tmp_path: Path):
    _, entries = gcm.parse_crontab(CRONTAB)
    commands = " ".join(e.command for e in entries)
    assert "retired_job.sh" not in commands, (
        "a commented-out crontab line is not a schedule; documenting it is how "
        "CRON_MAP came to describe six scripts that no longer exist"
    )
    assert len(entries) == 4


def test_wrapper_assignment_is_kept_as_a_note():
    """`W=` is not a job, but every job line is meaningless without it."""
    assignments, _ = gcm.parse_crontab(CRONTAB)
    assert "W=/srv/genus/scripts/cron-wrapper.sh" in assignments


def test_missing_script_is_flagged(tmp_path: Path):
    ws = workspace(tmp_path)
    out = gcm.render(crontab_text=crontab_for(ws), timers_text="", schedules=None, workspace=ws)
    gone = next(line for line in out.splitlines() if "gone.sh" in line)
    present = next(line for line in out.splitlines() if "present.sh" in line)
    assert "MISSING" in gone, gone
    assert "MISSING" not in present, present


def test_target_is_resolved_against_the_lines_own_cd(tmp_path: Path):
    """`cd X && ... y.py` runs y.py in X, not in the workspace. Resolving it
    against the workspace would report a live job as MISSING — a false alarm
    is how a report stops being read."""
    ws = workspace(tmp_path)
    out = gcm.render(crontab_text=crontab_for(ws), timers_text="", schedules=None, workspace=ws)
    nested = next(line for line in out.splitlines() if "nested.py" in line)
    assert "MISSING" not in nested, nested


def test_unexpanded_cd_variable_is_not_resolved(tmp_path: Path):
    """`cd $W && ./x.sh` cannot be resolved: $W is a crontab assignment this
    generator does not expand. Resolving `./x.sh` against the workspace anyway
    invents a directory the job never ran in, and prints either a MISSING that
    is false or an `ok` that was never checked."""
    ws = workspace(tmp_path)
    text = "0 4 * * * cd $W && ./x.sh\n"
    out = gcm.render(crontab_text=text, timers_text="", schedules=None, workspace=ws)
    line = next(line for line in out.splitlines() if "x.sh" in line)
    assert "MISSING" not in line, line
    assert "ok" not in line, (
        "an unresolvable cd target was reported as present — a positive fact "
        f"nothing checked\n{line}"
    )


# ── systemd timers ───────────────────────────────────────────────────────────


def test_timers_are_filtered_to_robothor_and_delphi():
    rows = gcm.parse_timers(TIMERS)
    units = [r.unit for r in rows]
    assert "apt-daily.timer" not in units
    assert "robothor-liveness.timer" in units
    assert "delphi-report.timer" in units


def test_timers_are_sorted_by_next_run():
    rows = gcm.parse_timers(TIMERS)
    units = [r.unit for r in rows]
    assert units == [
        "robothor-backup-local.timer",  # 04:00
        "robothor-liveness.timer",  # 16:00
        "delphi-report.timer",  # no next run at all — last
    ], units


def test_timer_with_no_next_run_is_shown_as_such():
    rows = gcm.parse_timers(TIMERS)
    stopped = next(r for r in rows if r.unit == "delphi-report.timer")
    assert stopped.next_run == "-"
    assert stopped.activates == "delphi-report.service"


# ── sections ─────────────────────────────────────────────────────────────────


def test_all_three_sections_are_present(tmp_path: Path):
    ws = workspace(tmp_path)
    out = gcm.render(
        crontab_text=crontab_for(ws),
        timers_text=TIMERS,
        schedules=SCHEDULES,
        workspace=ws,
    )
    assert "## Crontab" in out
    assert "## Systemd timers" in out
    assert "## Agent schedules" in out
    assert "crm-hygiene" in out
    assert "robothor-liveness.timer" in out


def test_agent_schedules_section_says_when_it_was_skipped(tmp_path: Path):
    """`--no-db` must produce a section that says the rows were NOT read, not
    an empty table that reads as "no agents are scheduled"."""
    ws = workspace(tmp_path)
    out = gcm.render(
        crontab_text=crontab_for(ws), timers_text=TIMERS, schedules=None, workspace=ws
    )
    section = out.split("## Agent schedules", 1)[1]
    assert "not read" in section.lower() or "skipped" in section.lower(), section


def test_a_failed_crontab_read_is_not_an_empty_crontab(tmp_path: Path):
    """`crontab -l` failing and the operator having no cron jobs are different
    facts. `None` means NOT READ, exactly as it does for the schedule rows."""
    ws = workspace(tmp_path)
    out = gcm.render(crontab_text=None, timers_text="", schedules=None, workspace=ws)
    section = out.split("## Crontab", 1)[1].split("## Systemd timers", 1)[0]
    assert "No active crontab entries." not in section, section
    assert "not read" in section.lower(), section


def test_a_failed_timers_read_is_not_no_timers(tmp_path: Path):
    ws = workspace(tmp_path)
    out = gcm.render(crontab_text="", timers_text=None, schedules=None, workspace=ws)
    section = out.split("## Systemd timers", 1)[1].split("## Agent schedules", 1)[0]
    assert "No `robothor-*` or `delphi-*` timers." not in section, section
    assert "not read" in section.lower(), section


# ── CLI ──────────────────────────────────────────────────────────────────────


def run_cli(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, str(GEN), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
        env=env,
    )


def failing_stub(tmp_path: Path, name: str) -> dict[str, str]:
    """An env whose PATH finds a `name` that exits 1 — a box with no crontab,
    or one whose systemd is not answering."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(f"#!/usr/bin/env bash\necho '{name}: boom' >&2\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env


def test_cli_renders_from_files_without_a_database(tmp_path: Path):
    ws = workspace(tmp_path)
    cron_file = tmp_path / "crontab.txt"
    cron_file.write_text(crontab_for(ws))
    timers_file = tmp_path / "timers.txt"
    timers_file.write_text(TIMERS)

    result = run_cli(
        tmp_path,
        "--crontab-file",
        str(cron_file),
        "--timers-file",
        str(timers_file),
        "--workspace",
        str(ws),
        "--no-db",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "## Crontab" in result.stdout
    assert "## Systemd timers" in result.stdout
    assert "MISSING" in result.stdout


def test_cli_writes_nothing_to_disk(tmp_path: Path):
    """Output is stdout only — docs/CRON_MAP.md is gitignored instance data
    and the redirect belongs to the caller, not to this script."""
    ws = workspace(tmp_path)
    cron_file = tmp_path / "crontab.txt"
    cron_file.write_text(crontab_for(ws))
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    result = run_cli(
        tmp_path, "--crontab-file", str(cron_file), "--timers-file", "/dev/null",
        "--workspace", str(ws), "--no-db",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert before == after, "gen_cron_map.py must not create files"


def test_cli_exits_nonzero_when_the_crontab_cannot_be_read(tmp_path: Path):
    """A generator that fails to read an input, prints a positive fact about
    it and exits 0 puts that fact into CRON_MAP.md with nothing to catch it.
    The exit code is the only thing the redirecting caller can check."""
    ws = workspace(tmp_path)
    result = run_cli(
        tmp_path, "--timers-file", "/dev/null", "--workspace", str(ws), "--no-db",
        env=failing_stub(tmp_path, "crontab"),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    section = result.stdout.split("## Crontab", 1)[1].split("## Systemd", 1)[0]
    assert "No active crontab entries." not in section, section


def test_cli_exits_nonzero_when_the_timers_cannot_be_read(tmp_path: Path):
    ws = workspace(tmp_path)
    cron_file = tmp_path / "crontab.txt"
    cron_file.write_text(crontab_for(ws))
    result = run_cli(
        tmp_path, "--crontab-file", str(cron_file), "--workspace", str(ws), "--no-db",
        env=failing_stub(tmp_path, "systemctl"),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    section = result.stdout.split("## Systemd timers", 1)[1]
    assert "No `robothor-*` or `delphi-*` timers." not in section, section


def test_no_instance_data_in_the_generator():
    """CLAUDE.md rule 1 — the workspace comes from the env, never a literal."""
    text = GEN.read_text()
    assert "/home/" not in text
    assert "ROBOTHOR_WORKSPACE" in text


# ── the workspace default ────────────────────────────────────────────────────


def test_an_explicit_workspace_does_not_need_a_resolvable_home(monkeypatch):
    """The fallback must not be evaluated when it is not the answer.

    ``Path.home()`` sat inside ``os.environ.get(..., Path.home() / "robothor")``
    — an argument, so Python computes it on every call, ROBOTHOR_WORKSPACE set
    or not. Under a systemd unit with no HOME and no passwd entry for the
    service user that raises RuntimeError, and the generator dies on a box
    that had told it exactly where the workspace is.
    """

    def _no_home() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", "/srv/genus")
    assert gcm.default_workspace() == Path("/srv/genus")


def test_an_empty_workspace_variable_falls_back_rather_than_returning_nothing(
    monkeypatch,
):
    """`ROBOTHOR_WORKSPACE=` in an env file is unset, not "the root of the
    filesystem" — Path("") is Path(".")."""
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", "")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/srv/alice")))
    assert gcm.default_workspace() == Path("/srv/alice/robothor")


def test_the_fallback_is_still_the_home_workspace(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/srv/alice")))
    assert gcm.default_workspace() == Path("/srv/alice/robothor")
