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


def check_dropin_drift() -> None:
    """Surface divergence between the live systemd drop-in and its repo mirror.

    The drop-in is the production guardrail posture; an unversioned live edit
    must show up in the daily report rather than silently persist.
    """
    script = Path(__file__).resolve().parent / "check_dropin_drift.sh"
    if not script.exists():
        return
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
    print("\n=== drop-in drift check ===")
    print(result.stdout.rstrip())


def main() -> int:
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
    check_soak_deadlines()
    check_dropin_drift()
    return 0


if __name__ == "__main__":
    sys.exit(main())
