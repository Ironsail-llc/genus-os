#!/usr/bin/env python3
"""Weekly what-actually-shipped report for auto-agent + auto-researcher.

Answers: of the tasks these agents resolved this week, how many actually
shipped a harness/code change vs. were no-ops, stale resolves, or blockers?

Classification is **correlation-based** (no schema changes to crm_tasks):

- **shipped** — task run logged an experiment_commit tool call with verdict=keep.
- **stale** — resolution text contains "stale", "already met", "superseded",
  "already resolved".
- **blocked** — tagged 'blocked' OR resolution mentions "BLOCKED" / "broken".
- **no-op** — resolved without any of the above signals.

Delivery: plain-text Telegram message via the Bot API (same path as
scripts/devops_send_telegram.py). Runs Sunday 20:00 ET via crontab.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}
TENANT_ID = "robothor-primary"
AGENTS = ("auto-agent", "auto-researcher")
WINDOW_DAYS = 7
TELEGRAM_MAX_CHARS = 4000


def classify(task: dict, shipped_experiment_ids: set[str]) -> str:
    """Return one of: shipped, stale, blocked, no-op.

    Since crm_tasks has no agent_run_id column, we correlate by
    substring-matching any of this week's committed experiment IDs against the
    task's resolution text. False positives are possible (agent can mention an
    experiment without that being what the task shipped) but false negatives
    are worse, so we bias toward counting matches.
    """
    tags = task.get("tags") or []
    resolution_raw = task.get("resolution") or ""
    resolution = resolution_raw.lower()
    for exp_id in shipped_experiment_ids:
        if exp_id and exp_id.lower() in resolution:
            return "shipped"
    if "blocked" in tags or "BLOCKED" in resolution_raw or "broken" in resolution:
        return "blocked"
    for marker in ("stale", "already met", "superseded", "already resolved"):
        if marker in resolution:
            return "stale"
    return "no-op"


def _resolved_tasks(conn) -> list[dict]:
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, title, resolution, tags, assigned_to_agent, resolved_at
        FROM crm_tasks
        WHERE tenant_id = %s
          AND assigned_to_agent = ANY(%s)
          AND resolved_at IS NOT NULL
          AND resolved_at > NOW() - INTERVAL %s
        ORDER BY resolved_at DESC
        """,
        (TENANT_ID, list(AGENTS), f"{WINDOW_DAYS} days"),
    )
    return list(cur.fetchall())


def _shipping_runs(conn) -> dict[str, list[dict]]:
    """Find agent_runs whose steps called experiment_commit with verdict=keep.

    Returns a map agent_id -> list of {run_id, experiment_id, improvement_pct,
    tool_call_at, hypothesis}. Keyed by agent_id so we can list separately.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT
            ar.agent_id,
            ar.id AS run_id,
            s.tool_input,
            s.created_at AS called_at
        FROM agent_runs ar
        JOIN agent_run_steps s ON s.run_id = ar.id
        WHERE ar.agent_id = ANY(%s)
          AND s.tool_name = 'experiment_commit'
          AND s.step_type = 'tool_call'
          AND s.created_at > NOW() - INTERVAL %s
          AND ar.tenant_id = %s
        ORDER BY s.created_at DESC
        """,
        (list(AGENTS), f"{WINDOW_DAYS} days", TENANT_ID),
    )
    runs: dict[str, list[dict]] = {a: [] for a in AGENTS}
    for row in cur.fetchall():
        ti = row["tool_input"] or {}
        if ti.get("verdict") != "keep":
            continue
        runs[row["agent_id"]].append(
            {
                "run_id": str(row["run_id"]),
                "experiment_id": ti.get("experiment_id", "?"),
                "hypothesis": (ti.get("hypothesis") or "")[:80],
                "called_at": row["called_at"],
            }
        )
    return runs


def _run_stats(conn) -> dict[str, dict]:
    """Per-agent run counts and timeout rate for the window."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT agent_id, status, count(*) AS n
        FROM agent_runs
        WHERE tenant_id = %s
          AND agent_id = ANY(%s)
          AND parent_run_id IS NULL
          AND started_at > NOW() - INTERVAL %s
        GROUP BY agent_id, status
        """,
        (TENANT_ID, list(AGENTS), f"{WINDOW_DAYS} days"),
    )
    stats: dict[str, dict] = {
        a: {"total": 0, "timeout": 0, "completed": 0, "failed": 0} for a in AGENTS
    }
    for row in cur.fetchall():
        a = row["agent_id"]
        stats[a]["total"] += row["n"]
        if row["status"] in stats[a]:
            stats[a][row["status"]] = row["n"]
    return stats


def _open_follow_ups(conn) -> int:
    """Count unresolved LEARNING FOLLOW-UP tasks."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(*) FROM crm_tasks
        WHERE tenant_id = %s
          AND resolved_at IS NULL
          AND title LIKE 'LEARNING FOLLOW-UP:%%'
        """,
        (TENANT_ID,),
    )
    return cur.fetchone()[0]


def format_report(data: dict) -> str:
    lines: list[str] = [f"🛠 Weekly AutoAgent/Researcher Report — {data['week_ending']}"]
    lines.append("")
    for agent in AGENTS:
        stats = data["run_stats"][agent]
        classes = data["classes"][agent]
        total = sum(classes.values())
        timeout_rate = (stats["timeout"] / stats["total"] * 100) if stats["total"] else 0
        lines.append(f"▸ {agent}")
        lines.append(
            f"  runs: {stats['total']}  ({stats['completed']} ok, {stats['timeout']} timeout = {timeout_rate:.0f}%)"
        )
        lines.append(
            f"  resolved: {total}  →  {classes['shipped']} shipped · {classes['no-op']} no-op · {classes['stale']} stale · {classes['blocked']} blocked"
        )
        ships = data["shipping_runs"][agent][:3]
        lines.extend(f"    ✓ {s['experiment_id']}: {s['hypothesis']}" for s in ships)
    lines.append("")
    lines.append(f"Open learning follow-ups: {data['open_follow_ups']}")
    lines.append("")
    lines.append("Full task list in agent_memory_blocks — ask me to unpack any row.")
    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_MAX_CHARS:
        msg = msg[: TELEGRAM_MAX_CHARS - 20] + "\n\n[truncated]"
    return msg


def send_telegram(token: str, chat_id: str, text: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {e.code}: {body}") from e


def gather(conn) -> dict:
    tasks = _resolved_tasks(conn)
    shipping = _shipping_runs(conn)
    shipped_experiment_ids = {s["experiment_id"] for rs in shipping.values() for s in rs}

    classes: dict[str, dict[str, int]] = {
        a: {"shipped": 0, "stale": 0, "blocked": 0, "no-op": 0} for a in AGENTS
    }
    for t in tasks:
        if t["assigned_to_agent"] not in AGENTS:
            continue
        classes[t["assigned_to_agent"]][classify(t, shipped_experiment_ids)] += 1

    return {
        "week_ending": datetime.now(UTC).date().isoformat(),
        "run_stats": _run_stats(conn),
        "classes": classes,
        "shipping_runs": shipping,
        "open_follow_ups": _open_follow_ups(conn),
        "tasks_scanned": len(tasks),
    }


def main() -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        data = gather(conn)
    finally:
        conn.close()

    msg = format_report(data)
    print(msg)
    print("---")
    print(json.dumps(data, indent=2, default=str))

    # Telegram delivery is optional; skip if env not set (e.g. manual dry runs).
    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("Telegram env not set; skipping delivery.")
        return 0

    result = send_telegram(token, chat_id, msg)
    if not result.get("ok"):
        logger.error("Telegram send failed: %s", result)
        return 3
    logger.info(
        "Delivered to chat %s (msg_id=%s)", chat_id, result.get("result", {}).get("message_id")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
