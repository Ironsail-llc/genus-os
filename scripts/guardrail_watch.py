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
    ("/usr/local/bin/robothor-pg-basebackup.sh", "scripts/pg-basebackup.sh"),
    ("/usr/local/bin/robothor-wal-offsite.sh", "scripts/wal-offsite.sh"),
    ("/usr/local/bin/robothor-wal-archive.sh", "scripts/wal-archive.sh"),
    # The thermal guard is a SAFETY control (Aug 2026 GPU event) that ran for
    # weeks with no repo mirror at all — a rebuilt box would have lost it.
    ("/usr/local/bin/robothor-thermal-guard.sh", "scripts/thermal-guard.sh"),
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
