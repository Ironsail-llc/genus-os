#!/usr/bin/env python3
"""Guardrail soak monitor — surfaces would-block ("observed") counts per
guardrail so observe→enforce promotions are data-driven, plus the run
error/timeout rate. Run ad hoc or on a timer during a soak.

A guardrail is safe to flip to enforce when its `observed` count over a full
cron cycle is either 0 (injection_scan, sandbox_default with no host-fs agent)
or a hand-verified true-positive set (exec_allowlist). RBAC is already enforce.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from robothor.db.connection import get_connection

WINDOW_HOURS = int(os.environ.get("GUARDRAIL_WATCH_HOURS", "48"))


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
    check_dropin_drift()
    return 0


if __name__ == "__main__":
    sys.exit(main())
