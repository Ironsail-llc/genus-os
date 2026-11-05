#!/usr/bin/env python3
"""Scope-decision gate for auto-researcher (Phase 4 of the 2026-04-23 overhaul).

Runs AFTER a fresh engine restart so the new `update_task` / `append_to_block`
tools are in effect. Verifies that the three
preconditions from the plan hold:

1. ``experiment_measure`` can run a psql-based metric_command and return a
   numeric value.
2. An agent-side call to ``update_task`` can write ``Actual (latest): <value>``
   into a parent task body.
3. A narrow end-to-end experiment completes without timing out.

If any check fails, the script exits non-zero and emits a reason. Use the
result to decide whether to keep auto-researcher running (``enabled: true`` in
its manifest) or pause it.

This is a *read-mostly* checker — it does NOT launch an agent run. It instead
probes the infrastructure (a metric command through the real runner, tool
availability in the registry). The "completes without timing out" leg is
validated by inspecting the next scheduled run after the restart; this script
prints the gate command to follow up with.
"""

from __future__ import annotations

import os
import sys

import psycopg2


def check_psql_metric_command() -> tuple[bool, str]:
    """Precondition 1 — a psql metric command returns a number.

    Through ``experiment_measure``'s own runner, so it sees the environment a
    metric command really gets. That environment holds no database credential
    on any rung (no ``DATABASE_URL``, no password), so this passes only where
    psql can connect to the configured database without one, such as peer
    authentication on the local socket.
    """
    import shlex

    from robothor.config import get_config
    from robothor.engine.tools.handlers.experiment import _run_metric_command

    # psql reads none of the ROBOTHOR_DB_* settings, and without a name it
    # connects to the database named after the user, so the command names the
    # configured database, host, port and user.
    db = get_config().db
    target = [f"-d {shlex.quote(db.name)}", f"-p {int(db.port)}"]
    if db.host:
        target.append(f"-h {shlex.quote(db.host)}")
    if db.user:
        target.append(f"-U {shlex.quote(db.user)}")
    query = '"SELECT count(*) FROM agent_memory_blocks;"'
    command = f"psql {' '.join(target)} -t -A -c {query}"
    try:
        output = _run_metric_command(command, os.environ.get("ROBOTHOR_WORKSPACE", ""))
    except Exception as exc:  # noqa: BLE001 - the gate reports, it does not raise
        return False, str(exc)[:200]
    try:
        int(output.strip())
    except ValueError:
        return False, f"psql output is not numeric: {output!r}"
    return True, f"count={output.strip()}"


def check_tool_registry() -> tuple[bool, str]:
    """Precondition 2 — update_task and append_to_block are in the tool registry
    AND auto-researcher's manifest includes them."""
    from pathlib import Path

    import yaml

    with Path("docs/agents/auto-researcher.yaml").open() as f:
        manifest = yaml.safe_load(f)
    allowed = set(manifest.get("tools_allowed") or [])
    missing = {"update_task", "append_to_block"} - allowed
    if missing:
        return False, f"auto-researcher tools_allowed missing: {sorted(missing)}"

    from robothor.engine.tools.handlers import (
        crm,  # noqa: F401
        memory,  # noqa: F401
    )
    from robothor.engine.tools.handlers.experiment import HANDLERS as _EH  # noqa: F401

    # Both handlers register at import via @_handler; check the crm + memory
    # modules have them.
    try:
        from robothor.engine.tools.handlers.crm import HANDLERS as _CRM_H

        assert "update_task" in _CRM_H
        from robothor.engine.tools.handlers.memory import HANDLERS as _MEM_H

        assert "append_to_block" in _MEM_H
    except (ImportError, AssertionError, AttributeError) as exc:
        return False, f"handler registry check failed: {exc}"

    return True, "update_task + append_to_block registered and in allowlist"


def check_recent_auto_researcher_health(conn) -> tuple[bool, str]:
    """Precondition 3 — last 7d of auto-researcher runs show <30% timeout rate.

    Soft check: the strict gate is "one post-restart run completes", which
    this script cannot force. This gives a useful signal for the historical
    baseline.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE status = 'timeout') AS timeouts,
            count(*) FILTER (WHERE status = 'completed') AS completed
        FROM agent_runs
        WHERE agent_id = 'auto-researcher'
          AND parent_run_id IS NULL
          AND started_at > NOW() - INTERVAL '7 days'
        """
    )
    row = cur.fetchone()
    total, timeouts, completed = row
    if not total:
        return False, "no auto-researcher runs in last 7d"
    rate = timeouts / total
    if rate >= 0.30:
        return False, f"timeout rate {rate:.0%} on {total} runs (target <30%)"
    return True, f"timeout rate {rate:.0%} on {total} runs ({completed} completed)"


def main() -> int:
    print("=== Auto-Researcher Scope Gate ===\n")
    from robothor.config import get_config

    conn = psycopg2.connect(**get_config().db.dict)

    results: list[tuple[str, bool, str]] = []

    ok, msg = check_psql_metric_command()
    results.append(("psql metric command returns numeric", ok, msg))

    ok, msg = check_tool_registry()
    results.append(("tool registry + manifest allowlist", ok, msg))

    ok, msg = check_recent_auto_researcher_health(conn)
    results.append(("recent timeout rate < 30%", ok, msg))
    conn.close()

    for label, ok, msg in results:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"{mark} {label}: {msg}")

    all_passed = all(ok for _, ok, _ in results)
    print()
    if all_passed:
        print(
            "All preconditions hold. Keep auto-researcher enabled. "
            "Next step: after the next scheduled auto-researcher run, verify "
            "that at least one parent task's body now contains "
            "'Actual (latest): <number>' — confirms the tools are used."
        )
        return 0

    failed = [label for label, ok, _ in results if not ok]
    print(
        f"Failed: {failed}. Recommend pausing auto-researcher by setting "
        "'enabled: false' in docs/agents/auto-researcher.yaml "
        "and filing a follow-up task for the failing preconditions."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
