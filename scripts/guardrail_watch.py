#!/usr/bin/env python3
"""Guardrail soak monitor — surfaces would-block ("observed") counts per
guardrail so observe→enforce promotions are data-driven, plus the run
error/timeout rate and soak-deadline nags. Run ad hoc or on a timer.

A guardrail is safe to flip to enforce when its `observed` count over a full
cron cycle is either 0 (injection_scan, sandbox_default with no host-fs agent)
or a hand-verified true-positive set (exec_allowlist). RBAC is already enforce.

Flags and their planned promotion dates live in infra/flags.yaml; any flag
still in observe/alert past its date is nagged here daily (stdout always,
Telegram when ROBOTHOR_TELEGRAM_BOT_TOKEN/CHAT_ID are configured) so a "48h
soak" can never silently become a 44-day one again.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

import yaml

WINDOW_HOURS = int(os.environ.get("GUARDRAIL_WATCH_HOURS", "48"))
REPO_ROOT = Path(__file__).resolve().parents[1]
FLAG_MANIFEST = REPO_ROOT / "infra" / "flags.yaml"

# Modes that are pre-promotion: sitting in one past the planned date is debt.
PENDING_MODES = ("observe", "alert")


def _today() -> dt.date:
    return dt.datetime.now(tz=dt.UTC).date()


def load_manifest(path: Path = FLAG_MANIFEST) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("flags", [])


def overdue_flags(flags: list[dict], today: dt.date | None = None) -> list[dict]:
    """Flags still in a pre-promotion mode past their planned_promotion date."""
    today = today or _today()
    overdue = []
    for entry in flags:
        if entry.get("mode") not in PENDING_MODES:
            continue
        planned = entry.get("planned_promotion")
        if not planned:
            continue
        if dt.date.fromisoformat(str(planned)) < today:
            overdue.append(entry)
    return overdue


def format_nag(overdue: list[dict], today: dt.date | None = None) -> str:
    if not overdue:
        return ""
    today = today or _today()
    lines = ["⚠️ FLAG SOAK OVERDUE — promote or re-plan (docs/runbooks/GUARDRAIL_FLIPS.md):"]
    for entry in overdue:
        planned = dt.date.fromisoformat(str(entry["planned_promotion"]))
        days = (today - planned).days
        lines.append(
            f"  {entry['name']}: {entry['mode']} — {days}d past planned "
            f"promotion {planned.isoformat()} (owner: {entry.get('owner', '?')})"
        )
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.load(resp).get("ok"))
    except Exception as exc:  # nag delivery is best-effort; the report still prints
        print(f"  (telegram nag failed: {exc})", file=sys.stderr)
        return False


def check_soak_deadlines() -> None:
    nag = format_nag(overdue_flags(load_manifest()))
    print("\n=== flag soak deadlines ===")
    if not nag:
        print("  OK — no flag is past its planned promotion date")
        return
    print(nag)
    if send_telegram(nag):
        print("  (nag sent to Telegram)")


def _stderr_tail(text: str, *, lines: int = 3, chars: int = 400) -> str:
    """The last few stderr lines, one line, bounded — the part that names the
    failure. Empty stderr yields ``"(no stderr)"`` rather than a blank space,
    so a message never reads as if the tail were the explanation."""
    tail = " | ".join(part.strip() for part in text.strip().splitlines()[-lines:] if part.strip())
    if not tail:
        return "(no stderr)"
    return tail[-chars:]


def _flag_audit_could_not_run(rc: str, detail: str) -> bool:
    """Report a DEAD audit as dead, and page with that wording.

    Distinct from drift on purpose. Until 2026-09 every non-zero rc paged
    "FLAG LAYERS DISAGREE", so an audit that crashed on an import error, a
    missing infra/flags.yaml or a drifted evidence schema sent the operator to
    stare at flags that were fine — and the stderr that said what actually
    broke was captured and then thrown away, printed nowhere. Still returns
    False: a watchdog whose probe died must not report health.
    """
    nag = f"⚠️ flag audit could not run (rc={rc}): {detail}"
    print(f"  FAIL: {nag}")
    if send_telegram(nag):
        print("  (nag sent to Telegram)")
    return False


def check_flag_truth(*, no_db: bool = False, timeout: int = 180) -> bool:
    """Print the per-flag truth table and fail when a layer is shadowed.

    ``check_soak_deadlines`` above nags about the manifest's *intent*;
    ``check_dropin_drift`` below compares the drop-in against its repo mirror.
    Neither answers the question an operator actually has — *which layer is
    governing this flag in the process that is running right now* — and the
    gap is not theoretical: this instance runs ADMISSION at ``enforce`` from
    ``/etc/robothor/robothor.env`` while ``infra/flags.yaml`` records
    ``observe``, and a dozen ``feature_flags`` rows pin flags over both.

    Run as a subprocess, like ``check_instance_manifests``: the audit imports
    the flag store and touches the database, and a crash in that import must
    fail THIS CHECK rather than take the whole watch down. Returns False on
    drift so ``main()`` exits non-zero and the unit's ``OnFailure=`` pager
    fires. The audit is read-only — SELECTs only, no writes anywhere.

    ``no_db=True`` runs it as a DB-free check (``--no-db``): the file layers
    alone answer "which layer governs this flag", and that half of the watch
    must survive a postgres outage. The second, DB-backed pass is what sees a
    ``feature_flags`` pin and the evidence columns.

    Exactly one non-zero code means drift: rc=1 *with a table on stdout*, the
    audit's own verdict. Anything else is the audit dying, and
    :func:`_flag_audit_could_not_run` says so instead of crying wolf.
    """
    script = Path(__file__).resolve().parent / "flag_audit.py"
    print(f"\n=== flag truth table ({'file layers only' if no_db else 'with DB evidence'}) ===")
    if not script.exists():
        print("  FAIL: flag_audit.py missing — the layer audit could not run")
        return False
    cmd = [sys.executable, str(script)]
    if no_db:
        cmd.append("--no-db")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _flag_audit_could_not_run("?", f"{type(exc).__name__}: {exc}")

    for line in result.stdout.rstrip().splitlines():
        print(f"  {line}")
    # Print it, always. Captured-and-discarded stderr is how a crash looked
    # exactly like a disagreement from the report alone.
    for line in result.stderr.rstrip().splitlines():
        print(f"  stderr: {line}")

    if result.returncode == 0:
        return True
    if result.returncode != 1 or not result.stdout.strip():
        return _flag_audit_could_not_run(str(result.returncode), _stderr_tail(result.stderr))
    nag = (
        "⚠️ FLAG LAYERS DISAGREE — a flag is set in more than one "
        "place, or the running engine does not match infra/flags.yaml. "
        "Run scripts/flag_audit.py for the table."
    )
    print(f"  {nag}")
    if send_telegram(nag):
        print("  (nag sent to Telegram)")
    return False


# A session goal that has not moved in this long is finished, wrong, or
# abandoned — all three deserve the operator's attention. Six of them sat in
# REVIEW for 2-5 weeks before anyone noticed (2026-07-13).
STALE_GOAL_DAYS = int(os.environ.get("GUARDRAIL_WATCH_STALE_GOAL_DAYS", "14"))


def stale_goals(
    goals: list[dict], today: dt.date | None = None, max_age_days: int = STALE_GOAL_DAYS
) -> list[dict]:
    """Session goals whose last update is older than the staleness window."""
    today = today or _today()
    out = []
    for g in goals:
        updated = g.get("updated")
        if not updated:
            continue
        if (today - updated).days > max_age_days:
            out.append(g)
    return out


def format_stale_goal_nag(stale: list[dict], today: dt.date | None = None) -> str:
    if not stale:
        return ""
    today = today or _today()
    lines = ["\u26a0\ufe0f STALE SESSION GOALS — finish, re-scope, or close:"]
    for g in stale:
        age = (today - g["updated"]).days
        lines.append(
            f"  [{g.get('agent', '?')}] {g.get('title', '?')[:60]} — "
            f"{age}d without movement ({g.get('status', '?')})"
        )
    return "\n".join(lines)


def check_stale_goals() -> None:
    """Surface session goals that have stopped moving."""
    from robothor.db.connection import get_connection

    print("\n=== stale session goals ===")
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT title,
                       COALESCE((SELECT t FROM unnest(tags) t WHERE t LIKE 'agent:%' LIMIT 1), '?'),
                       status,
                       updated_at::date
                FROM crm_tasks
                WHERE tags @> ARRAY['session_goal']
                  AND status NOT IN ('DONE', 'CANCELED')
                  AND deleted_at IS NULL
                """
            )
            goals = [
                {
                    "title": r[0],
                    "agent": str(r[1]).removeprefix("agent:"),
                    "status": r[2],
                    "updated": r[3],
                }
                for r in cur.fetchall()
            ]
    except Exception as exc:
        print(f"  (could not read session goals: {exc})")
        return

    nag = format_stale_goal_nag(stale_goals(goals))
    if not nag:
        print(f"  OK — no goal has been idle more than {STALE_GOAL_DAYS} days")
        return
    print(nag)
    if send_telegram(nag):
        print("  (nag sent to Telegram)")


# The repo mirror directory and its live counterpart. Every *.conf mirrored
# here gets a drift check automatically — adding a new mirrored drop-in is
# enough; there is no separate list to remember to update (unlike
# HOST_SCRIPT_DRIFT_PAIRS below, which has no directory to enumerate).
DROPIN_MIRROR_DIR = REPO_ROOT / "infra" / "systemd" / "robothor-engine.service.d"
DROPIN_LIVE_DIR = Path("/etc/systemd/system/robothor-engine.service.d")


def dropin_conf_pairs(
    mirror_dir: Path = DROPIN_MIRROR_DIR, live_dir: Path = DROPIN_LIVE_DIR
) -> list[tuple[str, str]]:
    """(live path, repo mirror path) for every *.conf file under mirror_dir.

    Kept separate from check_dropin_drift() so discovery is testable without
    touching /etc, per the injectable-pairs pattern below.
    """
    if not mirror_dir.exists():
        return []
    return [(str(live_dir / p.name), str(p)) for p in sorted(mirror_dir.glob("*.conf"))]


def check_dropin_drift(pairs: list[tuple[str, str]] | None = None) -> None:
    """Surface divergence between each live systemd drop-in and its repo mirror.

    Originally checked exactly one file (upgrade-rip-flags.conf); now iterates
    every *.conf mirrored under infra/systemd/robothor-engine.service.d/ (see
    dropin_conf_pairs), so hardening.conf and zz-sandbox.conf get the same
    coverage instead of drifting invisibly.

    The drop-in is the production guardrail posture; an unversioned live edit
    must show up in the daily report rather than silently persist.
    """
    script = Path(__file__).resolve().parent / "check_dropin_drift.sh"
    if not script.exists():
        return
    print("\n=== drop-in drift check ===")
    for live, mirror in pairs if pairs is not None else dropin_conf_pairs():
        result = subprocess.run(
            ["bash", str(script), live, mirror], capture_output=True, text=True, timeout=30
        )
        print(result.stdout.rstrip())


# (live path, repo-relative mirror path) — kept in sync by
# scripts/install-host-scripts.sh. A hand-copied script that drifts from its
# repo source is exactly how a month-old permission fix in pg-basebackup.sh
# stayed unapplied on the live box.
HOST_SCRIPT_DRIFT_PAIRS: list[tuple[str, str]] = [
    # Postgres runs this one straight out of /usr/local/bin via archive_command,
    # so the installed copy is the copy that executes.
    ("/usr/local/bin/robothor-wal-archive.sh", "scripts/wal-archive.sh"),
    # No pair for robothor-pg-basebackup.sh or robothor-wal-offsite.sh:
    # scripts/install-host-scripts.sh stopped mirroring them and now deletes
    # any left behind. Their units ExecStart the workspace copy, and the
    # scripts source sibling helpers /usr/local/bin does not have — so a mirror
    # could not run even if something tried. A drift pair for a file nothing
    # installs reports "missing" forever, and a permanently red check is one
    # the operator stops reading.
    # The thermal guard is a SAFETY control (Aug 2026 GPU event) that ran for
    # weeks with no repo mirror at all — a rebuilt box would have lost it.
    ("/usr/local/bin/robothor-thermal-guard.sh", "scripts/thermal-guard.sh"),
    # /etc/logrotate.d/robothor existed with NO repo source and covered one
    # glob, so brain/memory_system/logs/ reached 205 MB unrotated. A rotation
    # policy nothing checks is one edit away from being that gap again.
    ("/etc/logrotate.d/robothor", "infra/logrotate/robothor.conf"),
]


def check_host_script_drift(pairs: list[tuple[str, str]] | None = None) -> None:
    """Compare the installed host ops scripts under /usr/local/bin against
    their repo copies.

    These are hand-copied with no installer and no drift check today —
    scripts/install-host-scripts.sh is the fix for the copy step, this is the
    guard that says loudly when it hasn't been re-run since the repo changed.
    Reuses check_dropin_drift.sh, which already does exact-file comparison
    with the right exit codes and diff output for two arbitrary paths.
    """
    script = Path(__file__).resolve().parent / "check_dropin_drift.sh"
    if not script.exists():
        return
    print("\n=== host ops script drift check ===")
    for live, mirror in pairs if pairs is not None else HOST_SCRIPT_DRIFT_PAIRS:
        mirror_path = REPO_ROOT / mirror
        result = subprocess.run(
            ["bash", str(script), live, str(mirror_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(result.stdout.rstrip())


def check_instance_doctor(script: Path | None = None) -> bool:
    """Install truth: what is on this box that no repo template describes?

    install-units.sh reports installed/updated/unchanged — one direction only.
    The other direction (a unit symlinked into the checkout, nine live units
    with no template, inert .bak files in a drop-in directory, a hand drop-in,
    a service enabled but not running) was invisible to every command on the
    box. Needs no database, so it belongs in main()'s DB-free block.

    Returns False on any finding OR when the doctor itself could not run — a
    watchdog whose probe is missing must never report health.
    """
    doctor = script or (Path(__file__).resolve().parent / "instance_doctor.sh")
    print("\n=== instance install truth ===")
    if not Path(doctor).exists():
        print(f"  FAIL: {Path(doctor).name} missing — install truth was NOT checked")
        return False
    try:
        result = subprocess.run(
            ["bash", str(doctor)], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  FAIL: instance doctor could not run: {exc}")
        return False
    print(result.stdout.rstrip())
    if result.returncode not in (0, 1):
        print(f"  FAIL: instance doctor exited {result.returncode}\n{result.stderr.rstrip()}")
        return False
    return result.returncode == 0


def scoping_is_vacuous(non_privileged: int, linked_facts: int) -> bool:
    """True when a scoping guarantee is being advertised but cannot bind.

    Pure so the alarm condition itself is testable. A check that has only ever
    been observed staying silent is indistinguishable from one that cannot
    fire — which is the failure mode this whole sweep exists to catch.
    """
    return non_privileged > 0 and linked_facts == 0


def check_memory_scoping_is_not_vacuous() -> None:
    """Page if a non-owner role exists while memory scoping has nothing to filter on.

    ROBOTHOR_DATA_SCOPING=enforce restricts memory reads to rows where
    person_id matches the caller or is NULL. Nothing writes
    memory_facts.person_id — 0 of the last 5,521 facts carried one when this
    check was written — so the predicate admits essentially the whole corpus.

    That is harmless while every tenant_users row is owner/admin/service, since
    scope_for treats those as unrestricted. It stops being harmless the instant
    someone runs `robothor user add` with a lesser role: that user gets a memory
    read that looks scoped and is not, with no flag flip to notice.

    This converts that silent transition into a daily page. See
    docs/runbooks/IDENTITY_ROLLOUT.md.
    """
    from robothor.db.connection import get_connection

    privileged = ("owner", "admin", "service")
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT count(*) FROM tenant_users WHERE COALESCE(role, '') NOT IN %s",
                (privileged,),
            )
            row = cur.fetchone()
            non_privileged = int(row[0]) if row else 0

            cur.execute(
                "SELECT count(*), count(person_id) FROM memory_facts "
                "WHERE created_at > now() - interval '7 days'"
            )
            row = cur.fetchone()
            recent, linked = (int(row[0]), int(row[1])) if row else (0, 0)
        except Exception as e:  # table shape differs per instance — never fail the sweep
            print(f"\n=== memory scoping check ===\n  (skipped: {e})")
            return

    print("\n=== memory scoping check ===")
    print(f"  non-privileged tenant_users: {non_privileged}")
    print(f"  facts last 7d: {recent}, carrying person_id: {linked}")

    if scoping_is_vacuous(non_privileged, linked):
        msg = (
            f"MEMORY SCOPING IS VACUOUS: {non_privileged} non-privileged user(s) exist, "
            f"but 0 of the last {recent} facts carry a person_id. Those users get "
            f"unscoped memory reads that look scoped. See "
            f"docs/runbooks/IDENTITY_ROLLOUT.md before granting further access."
        )
        print(f"  <-- {msg}")
        send_telegram(msg)


# ── SLOs ─────────────────────────────────────────────────────────────────────
# The daily, non-paging surface for the reliability targets. scripts/slo_probe.sh
# is the hourly pager for the three that must interrupt someone; this section
# reports ALL of them and leaves exactly one alert_digest row for the heartbeat.
#
# The backup tier is measured from the last-good markers on NVMe, with no
# database involved, and it is reported FIRST. A database outage is one of the
# conditions under which an operator most needs to know the backup age, so that
# measurement must not be downstream of a connection — the same lesson main()
# learned about the drift checks on 2026-08-16.

#: The one spelling of the marker directory, shared with scripts/backup-state.sh.
BACKUP_STATE_DIR_DEFAULT = "/var/lib/robothor/backup-state"

#: The one implementation of the DB-free SLOs, shared with the hourly pager.
SLO_PROBE = REPO_ROOT / "scripts" / "slo_probe.sh"

#: marker file -> (label, budget env var, default hours). Nightly tiers get 26h
#: (a 24h cycle plus room for a late run); the base backup is weekly, and a
#: stale one costs restore TIME rather than data, so it carries a much wider
#: 8-day budget. The env var names are scripts/slo_probe.sh's own: a budget set
#: once in /etc/robothor/robothor.env has to move BOTH surfaces, or the daily
#: report measures something the dead-man does not.
BACKUP_SLO_BUDGETS: tuple[tuple[str, str, str, int], ...] = (
    ("last-local-dump", "S4 backup freshness: local dump", "ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS", 26),
    ("last-offsite-ok", "S4 backup freshness: offsite", "ROBOTHOR_SLO_OFFSITE_MAX_HOURS", 26),
    (
        "last-basebackup",
        "S4 backup freshness: basebackup",
        "ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS",
        192,
    ),
)

#: Statuses that are not an outcome. `cancelled` is an operator or scheduler
#: decision, not a failure (#438), and the non-terminal ones have not happened
#: yet — counting either as a denominator makes the success rate a measure of
#: how busy the box is.
_NON_OUTCOME_STATUSES = ("pending", "running", "cancelled", "skipped", "awaiting_approval")


class Slo(NamedTuple):
    """One reliability target and what this run actually measured for it.

    ``status`` is deliberately three-valued. "Could not evaluate" is NOT "OK":
    a check that has only ever been seen staying silent is indistinguishable
    from one that cannot fire, which is how six built-and-wired controls turned
    out to be inert.
    """

    name: str
    target: str
    measured: str
    status: str  # "OK" | "BREACH" | "UNEVALUATED"


def _marker_age_hours(path: Path, now: dt.datetime) -> float | None:
    """Hours since a backup-state marker, or None when it cannot be read.

    Format is scripts/backup-state.sh's: ``<date -Is> <identifier>``. An
    absent, empty or unparseable marker returns None — never a fresh-looking
    number. An absent marker reads as "recent" to anything that only checks for
    a non-empty string; it means the opposite.
    """
    try:
        first = path.read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    try:
        when = dt.datetime.fromisoformat(first.split(" ", 1)[0])
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return (now - when).total_seconds() / 3600


def budget_hours(env_var: str, default: int) -> int:
    """A budget from the environment, under scripts/slo_probe.sh's own name.

    An unparseable value falls back to the default and says so: a typo in
    robothor.env must never quietly widen a budget to infinity, which is a
    dead-man that reports every backup as fresh.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  ({env_var}={raw!r} is not an integer — using the {default}h default)")
        return default


def probe_report_slos(probe: Path | None = None) -> list[Slo]:
    """Run ``scripts/slo_probe.sh --report`` and render what IT measured.

    Deliberately a subprocess rather than a second implementation. This
    surface used to read the last-good markers only, while the probe takes the
    worse of (marker, newest file) and adds a readdir and a volume probe — and
    the 2026-08-27 volume drop is exactly the state those two answer
    differently. The markers live on NVMe and stay fresh forever, so the daily
    report said OK for two days while the pager said BREACH.

    Falls back to the marker-only reading when the probe cannot run, and says
    so: a daily report that goes silent because a shell script moved is the
    inert-control failure in a new costume.
    """
    probe = probe or SLO_PROBE
    if not probe.exists():
        print(f"  (SLO probe missing at {probe} — falling back to the markers alone)")
        return backup_freshness_slos()
    try:
        result = subprocess.run(
            ["bash", str(probe), "--report"], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  (SLO probe could not run: {exc} — falling back to the markers alone)")
        return backup_freshness_slos()

    out = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 5 and fields[0] == "SLO":
            out.append(Slo(fields[1], fields[2], fields[3], fields[4]))
    if not out:
        print(f"  (SLO probe reported nothing, exit {result.returncode} — falling back)")
        for line in result.stderr.splitlines()[-5:]:
            print(f"    {line}")
        return backup_freshness_slos()
    return out


def backup_freshness_slos(
    state_dir: Path | str | None = None, now: dt.datetime | None = None
) -> list[Slo]:
    """S4 from the markers alone — the fallback for when the probe cannot run.

    scripts/slo_probe.sh is the primary measurement (see probe_report_slos);
    this reads the same markers with the same budgets and no database, so a
    box whose probe is missing still gets an answer rather than a blank.
    """
    root = Path(
        state_dir
        if state_dir is not None
        else os.environ.get("ROBOTHOR_BACKUP_STATE_DIR", BACKUP_STATE_DIR_DEFAULT)
    )
    now = now or dt.datetime.now().astimezone()
    out = []
    for marker, label, env_var, default in BACKUP_SLO_BUDGETS:
        budget = budget_hours(env_var, default)
        target = f"< {budget}h"
        age = _marker_age_hours(root / marker, now)
        if age is None:
            out.append(Slo(label, target, "unknown — no successful run recorded", "BREACH"))
        else:
            status = "BREACH" if age > budget else "OK"
            out.append(Slo(label, target, f"{age:.0f}h", status))
    return out


def _pct(bad: int, total: int) -> str:
    return f"{100 * bad / total:.1f}% ({bad}/{total})" if total else "no runs"


def db_slos() -> list[Slo]:
    """The SLOs that need a read-only query. Never raises: an unreachable
    database yields UNEVALUATED rows, not missing ones."""
    from robothor.db.connection import get_connection

    placeholder = [
        Slo("S1 run success", "bad <= 5%", "", "UNEVALUATED"),
        Slo("S2 heartbeat delivery", ">= 95% delivered", "", "UNEVALUATED"),
        Slo("S3 pager delivery", "0 lost pages / 7d", "", "UNEVALUATED"),
        Slo("S6 LLM availability", "'all models failed' < 1%/day", "", "UNEVALUATED"),
        Slo("S7 workflows", "bad <= 10%", "", "UNEVALUATED"),
    ]
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            out = []

            # S1 — terminal outcomes only, benchmark harness runs excluded:
            # a benchmark suite deliberately drives agents into failure, so
            # counting it makes the fleet's reliability track the test plan.
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE status IN ('failed', 'timeout')), count(*)
                FROM agent_runs
                WHERE started_at >= now() - interval '7 days'
                  AND status <> ALL(%s)
                  AND COALESCE(trigger_detail, '') NOT LIKE 'benchmark:%%'
                """,
                (list(_NON_OUTCOME_STATUSES),),
            )
            bad, total = cur.fetchone()
            out.append(
                Slo(
                    "S1 run success",
                    "bad <= 5%",
                    _pct(bad, total),
                    "BREACH" if total and 100 * bad / total > 5 else "OK",
                )
            )

            # S2 — a heartbeat that ran but was never delivered is invisible to
            # the operator, which is the same as not having run.
            agent = os.environ.get("ROBOTHOR_SLO_HEARTBEAT_AGENT", "main")
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE delivered_at IS NOT NULL), count(*)
                FROM agent_runs
                WHERE started_at >= now() - interval '24 hours'
                  AND agent_id = %s AND trigger_detail LIKE 'heartbeat:%%'
                """,
                (agent,),
            )
            delivered, beats = cur.fetchone()
            out.append(
                Slo(
                    "S2 heartbeat delivery",
                    ">= 95% delivered",
                    f"{delivered}/{beats} in 24h",
                    "BREACH" if beats == 0 or 100 * delivered / beats < 95 else "OK",
                )
            )

            # S3 — every alert_fallback row is a page that was NOT delivered
            # and had to be left for the next briefing instead.
            cur.execute(
                """
                SELECT count(*) FROM crm_agent_notifications
                WHERE created_at >= now() - interval '7 days'
                  AND notification_type = 'alert_fallback'
                """
            )
            (lost,) = cur.fetchone()
            out.append(
                Slo(
                    "S3 pager delivery",
                    "0 lost pages / 7d",
                    f"{lost} alert_fallback rows",
                    "BREACH" if lost else "OK",
                )
            )

            # S6 — two different outages wear the same face here: every model
            # exhausted (one shared credential pool), and everything quietly
            # riding the local fallback tier.
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE error_message ILIKE '%%All models failed%%'),
                       count(*) FILTER (WHERE model_used LIKE 'ollama_chat/%%'),
                       count(*)
                FROM agent_runs
                WHERE started_at >= now() - interval '24 hours'
                """
            )
            all_failed, local, runs = cur.fetchone()
            share = 100 * all_failed / runs if runs else 0
            local_share = 100 * local / runs if runs else 0
            out.append(
                Slo(
                    "S6 LLM availability",
                    "'all models failed' < 1%/day, local fallback < 30%",
                    f"{share:.1f}% all-failed, {local_share:.0f}% local fallback ({runs} runs)",
                    "BREACH" if share >= 1 or local_share >= 30 else "OK",
                )
            )

            # S7 — per workflow, because one broken pipeline hides inside a
            # healthy fleet average.
            cur.execute(
                """
                SELECT workflow_id,
                       count(*) FILTER (WHERE status IN ('failed', 'timeout')), count(*)
                FROM workflow_runs
                WHERE started_at >= now() - interval '7 days'
                  AND status <> ALL(%s)
                GROUP BY workflow_id ORDER BY 1
                """,
                (list(_NON_OUTCOME_STATUSES),),
            )
            rows = cur.fetchall()
            worst = max(
                ((w, b, t) for w, b, t in rows if t), key=lambda r: r[1] / r[2], default=None
            )
            if worst is None:
                out.append(Slo("S7 workflows", "bad <= 10%", "no workflow runs", "OK"))
            else:
                workflow, bad, total = worst
                out.append(
                    Slo(
                        "S7 workflows",
                        "bad <= 10%",
                        f"worst: {workflow} {_pct(bad, total)}",
                        "BREACH" if 100 * bad / total > 10 else "OK",
                    )
                )
            return out
    except Exception as exc:
        print(f"  (SLO queries could not run: {exc})")
        return placeholder


def format_slo_report(slos: list[Slo]) -> str:
    # Spelled out, never abbreviated. "UNEVAL" is the kind of shorthand a
    # reader skims past as a variant of OK, and the whole point of the third
    # state is that it is not one.
    return "\n".join(
        f"  {s.status:<11} {s.name}: {s.measured or '-'} (target {s.target})" for s in slos
    )


def write_slo_digest(subject: str, body: str) -> bool:
    """One ``alert_digest`` row for the whole run, read by main's heartbeat.

    One row, not one per breach: four breached SLOs on a bad morning must not
    become four notification rows racing each other into the briefing.
    """
    try:
        from robothor.crm.dal import send_notification

        return bool(
            send_notification(
                from_agent="guardrail-watch",
                to_agent="main",
                notification_type="alert_digest",
                subject=subject,
                body=body,
            )
        )
    except Exception as exc:
        print(f"  (could not write the SLO digest row: {exc})")
        return False


def check_slos() -> None:
    """Report every SLO, then leave one digest row if any of them breached."""
    print("\n=== SLOs ===")
    # DB-free first, and unconditionally — see the module comment above. S4, S5
    # and S8 all come from the hourly probe's --report mode, so the daily
    # surface and the pager can never disagree about what they measured.
    slos = probe_report_slos()
    slos += db_slos()
    print(format_slo_report(slos))

    breached = [s for s in slos if s.status == "BREACH"]
    if not breached:
        print("  every evaluated SLO is inside target")
        return
    body = "\n".join(f"{s.name}: {s.measured} (target {s.target})" for s in breached)
    print(f"  <-- {len(breached)} SLO(s) breached; see docs/runbooks/SLOS.md")
    if write_slo_digest(f"SLO breach x{len(breached)}", body):
        print("  (digest row written for the heartbeat)")


def _run_db_dependent_checks() -> None:
    """Everything here needs a live database connection.

    Kept out of main()'s DB-free section deliberately: if this raises (DB
    down, not up yet at boot, ...), main() must still have already produced
    the drift-check output before this ever ran.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        print(f"=== guardrail events, last {WINDOW_HOURS}h ===")
        cur.execute(
            """
            SELECT guardrail_name, action, mode, COUNT(*)
            FROM agent_guardrail_events
            WHERE created_at >= now() - make_interval(hours => %s)
            GROUP BY guardrail_name, action, mode
            ORDER BY guardrail_name, action
            """,
            (WINDOW_HOURS,),
        )
        rows = cur.fetchall()
        if not rows:
            print("  (no guardrail events — nothing would-block; safe to enforce)")
        for name, action, mode, n in rows:
            flag = "  <-- would BLOCK on enforce" if action == "observed" else ""
            print(f"  {name:24} {action:10} {mode or '-':8} {n}{flag}")

        print(f"\n=== run outcomes, last {WINDOW_HOURS}h ===")
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM agent_runs
            WHERE started_at >= now() - make_interval(hours => %s)
            GROUP BY status ORDER BY 2 DESC
            """,
            (WINDOW_HOURS,),
        )
        total = 0
        bad = 0
        for status, n in cur.fetchall():
            total += n
            if status in ("failed", "timeout"):
                bad += n
            print(f"  {status:12} {n}")
        if total:
            print(f"  error+timeout rate: {100 * bad / total:.1f}%  ({bad}/{total})")

    check_stale_goals()
    check_memory_scoping_is_not_vacuous()


def check_instance_manifests(
    manifest_dir: Path | None = None,
    workspace: Path | None = None,
    script: Path | None = None,
) -> bool:
    """Validate every instance manifest on this box, daily.

    The strict validator's default mode checks git-TRACKED manifests only, and
    every real instance manifest is gitignored — so a box running 25 manifests
    had exactly one validated, in CI, where instance defects cannot appear. On
    2026-08-23 an unparseable main.yaml took the primary agent down for 3h48m
    and the validator had no opinion. Its first --instance run found two live
    fleet defects nothing had ever looked for.

    Returns False on any FAIL or parse failure so main() can exit non-zero and
    the unit's OnFailure= pager fires. Runs as a subprocess: the validator
    imports the engine's ToolRegistry, and a crash in that import must fail
    THIS CHECK, not the whole watch. A watchdog whose probe dies must not
    report health.
    """
    root = workspace or REPO_ROOT
    validator = script or (Path(__file__).resolve().parent / "validate_agents.py")
    print("\n=== instance manifest validation ===")
    if not validator.exists():
        print(f"  FAIL: validator missing at {validator.name} — could not run")
        return False
    cmd = [sys.executable, str(validator), "--instance", "--workspace", str(root)]
    if manifest_dir is not None:
        cmd += ["--manifest-dir", str(manifest_dir)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(root))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  FAIL: validator could not run: {exc}")
        return False
    # Reprint only the signal: parse failures, FAIL lines, and the summary.
    interesting = [
        line
        for line in result.stdout.splitlines()
        if "FAIL" in line or "SUMMARY" in line or "UNPARSEABLE" in line or ".yaml" in line
    ]
    for line in interesting[-30:]:
        print(f"  {line.strip()}")
    if result.returncode != 0:
        print("  FAIL: instance manifest validation failed (see lines above)")
        return False
    print("  ok: every instance manifest parses and passes schema checks")
    return True


def main() -> int:
    # DB-free checks run FIRST and unconditionally. 2026-08-16: this unit's
    # Persistent=true timer fired at boot before postgres was up. The
    # DB-dependent section used to run first in this function; get_connection()
    # raised, and the drift checks below — which need no database at all —
    # never ran. The drift watchdog was undetectably down: no exception
    # reached anyone, no report, nothing. A DB outage must never take the
    # DB-free checks down with it.
    check_soak_deadlines()
    # --no-db: the file layers alone answer "which layer governs this flag",
    # and a DB read here would put the same outage back in the DB-free half.
    flags_ok = check_flag_truth(no_db=True)
    check_dropin_drift()
    check_host_script_drift()
    doctor_ok = check_instance_doctor()
    check_slos()
    manifests_ok = check_instance_manifests()

    try:
        _run_db_dependent_checks()
        # Second pass, with the database. The `feature_flags` pin, its actor
        # and the evidence columns (rows_7d, last_fired, last_probe) exist
        # only here — and a DB pin beats every file layer the pass above can
        # see, so dropping this pass would make an unversioned pin invisible.
        # 60s, not 180: postgres has just answered the checks above.
        flags_ok = check_flag_truth(no_db=False, timeout=60) and flags_ok
    except Exception as exc:
        print(
            f"\n=== DATABASE UNAVAILABLE: {exc} ===\n"
            "guardrail-watch: the DB-dependent checks (guardrail events, run "
            "outcomes, stale goals, memory scoping) were skipped. The DB-free "
            "checks above (flag soak deadlines, drop-in drift, host-script "
            "drift) already ran and are valid — this is a partial report, "
            "not a silent skip. Exiting non-zero so systemd marks the run "
            "failed and OnFailure pages."
        )
        return 1

    if not manifests_ok or not flags_ok or not doctor_ok:
        # The fleet's manifests are the fleet; a guardrail whose effective mode
        # is not the one the manifest records is a control nobody is actually
        # running; and a box that no longer matches its templates is a box
        # nobody can rebuild. Any of the three must reach the operator; rc=1
        # fires the unit's OnFailure= pager. The way to silence a KNOWN
        # instance-only unit is instance-units.allow, not a swallowed exit code.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
