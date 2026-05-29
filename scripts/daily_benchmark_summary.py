#!/usr/bin/env python3
"""Daily benchmark summary — Telegram message at 6 PM ET.

Queries `benchmark_results` for today's runs (and the prior day for the
delta), formats a concise summary, and sends it to the operator.

What we report:
  • Per-agent pass-rate change since prior measurement
  • Agents that improved or regressed by ≥ 5 points
  • What auto-researcher worked on today (count of in-flight before/after pairs)
  • Newly-opened benchmark gaps (suite present but zero rows)

What we never report: cost. The operator was explicit — cost is observed,
never optimized, never surfaced as a metric.

Required env:
  ROBOTHOR_TELEGRAM_BOT_TOKEN  — bot token
  ROBOTHOR_TELEGRAM_CHAT_ID    — target chat
  ROBOTHOR_DB_NAME             — e.g. robothor_memory (defaults if missing)

Run via systemd timer at 18:00 ET. See infra/systemd/robothor-benchmark-summary.*
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg2

TELEGRAM_MAX_CHARS = 4000
TENANT_ID = os.environ.get("ROBOTHOR_TENANT_ID", "robothor-primary")
WORKSPACE = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))


def _conn():
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        user=os.environ.get("PG_USER", "philip"),
        password=os.environ.get("PG_PASSWORD"),
        dbname=os.environ.get("ROBOTHOR_DB_NAME", "robothor_memory"),
    )


def collect_summary() -> dict:
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)

    today_rows: list[dict] = []
    prior_rows: dict[str, float] = {}
    in_flight_experiments: list[str] = []

    # NOTE: the `pass_rate` column stores the partial-credit aggregate score
    # (a task only needs 0.70 to "pass"). The honest grade — and what the
    # benchmark_pass_rate goal metric now uses — is passed / total_cases.
    # We compute that here so the 6 PM summary matches the goal scoring.
    def _true_pass_rate(passed: int, total: int) -> float:
        return (passed / total) if total else 0.0

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (agent_id)
              agent_id, suite_id, run_at, total_cases, passed, failed,
              failures
            FROM benchmark_results
            WHERE tenant_id = %s
              AND run_at >= %s AND run_at <= %s
            ORDER BY agent_id, run_at DESC
            """,
            (TENANT_ID, start, end),
        )
        for row in cur.fetchall():
            agent_id, suite_id, run_at, total, passed, failed, failures = row
            if isinstance(failures, str):
                failures = json.loads(failures)
            today_rows.append(
                {
                    "agent_id": agent_id,
                    "suite_id": suite_id,
                    "run_at": run_at,
                    "total_cases": total,
                    "passed": passed,
                    "failed": failed,
                    "pass_rate": _true_pass_rate(passed, total),
                    "failing_ids": [f.get("case_id") for f in (failures or []) if f.get("case_id")],
                }
            )

        # Prior pass rate per agent (immediately before today's run).
        for r in today_rows:
            cur.execute(
                """
                SELECT passed, total_cases FROM benchmark_results
                WHERE tenant_id = %s
                  AND agent_id = %s
                  AND run_at < %s
                ORDER BY run_at DESC
                LIMIT 1
                """,
                (TENANT_ID, r["agent_id"], r["run_at"]),
            )
            row = cur.fetchone()
            if row and row[1]:
                prior_rows[r["agent_id"]] = _true_pass_rate(row[0], row[1])

        # Experiments touched today
        cur.execute(
            """
            SELECT DISTINCT experiment_id
            FROM benchmark_results
            WHERE tenant_id = %s
              AND experiment_id IS NOT NULL
              AND run_at >= %s
            ORDER BY experiment_id
            """,
            (TENANT_ID, start),
        )
        in_flight_experiments = [r[0] for r in cur.fetchall()]

    # Suite gaps: directories with suite.yaml but no row ever
    bench_dir = WORKSPACE / "docs" / "benchmarks"
    gap_agents: list[str] = []
    if bench_dir.exists():
        with _conn() as conn, conn.cursor() as cur:
            for child in sorted(bench_dir.iterdir()):
                if not child.is_dir() or not (child / "suite.yaml").exists():
                    continue
                cur.execute(
                    "SELECT 1 FROM benchmark_results WHERE tenant_id = %s AND agent_id = %s LIMIT 1",
                    (TENANT_ID, child.name),
                )
                if not cur.fetchone():
                    gap_agents.append(child.name)

    return {
        "today_rows": today_rows,
        "prior": prior_rows,
        "experiments": in_flight_experiments,
        "gaps": gap_agents,
    }


def format_message(s: dict) -> str:
    rows = s["today_rows"]
    if not rows and not s["gaps"]:
        return (
            "Benchmark summary — no benchmarks ran today. "
            "Check that benchmark-runner cron is enabled."
        )

    lines: list[str] = []
    lines.append(f"Benchmark summary — {datetime.now(UTC).strftime('%Y-%m-%d')}")
    lines.append("")

    if rows:
        improved: list[tuple[str, float, float]] = []
        regressed: list[tuple[str, float, float]] = []
        flat: list[dict] = []
        for r in rows:
            prior = s["prior"].get(r["agent_id"])
            if prior is None:
                flat.append(r)
                continue
            delta = r["pass_rate"] - prior
            if delta >= 0.05:
                improved.append((r["agent_id"], r["pass_rate"], delta))
            elif delta <= -0.05:
                regressed.append((r["agent_id"], r["pass_rate"], delta))
            else:
                flat.append(r)

        if regressed:
            lines.append("▼ Regressed (≥5 points)")
            regressed.sort(key=lambda x: x[2])
            for aid, pr, d in regressed:
                lines.append(f"  {aid}: {int(pr * 100)}% ({d * 100:+.0f}%)")
            lines.append("")

        if improved:
            lines.append("▲ Improved (≥5 points)")
            improved.sort(key=lambda x: -x[2])
            for aid, pr, d in improved:
                lines.append(f"  {aid}: {int(pr * 100)}% ({d * 100:+.0f}%)")
            lines.append("")

        # Worst 5 currently
        lines.append("Lowest pass rates today")
        for r in sorted(rows, key=lambda x: x["pass_rate"])[:5]:
            tail = f" (failing: {', '.join(r['failing_ids'][:2])})" if r["failing_ids"] else ""
            lines.append(
                f"  {r['agent_id']}: {r['passed']}/{r['total_cases']} ({int(r['pass_rate'] * 100)}%){tail}"
            )
        lines.append("")

    if s["experiments"]:
        lines.append(f"Auto-researcher experiments touched today: {', '.join(s['experiments'])}")
        lines.append("")
    else:
        lines.append("Auto-researcher: no experiments today.")
        lines.append("")

    if s["gaps"]:
        lines.append(f"Benchmark suite gaps (no row ever): {', '.join(s['gaps'])}")

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


def main() -> int:
    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram credentials missing", file=sys.stderr)
        return 2

    try:
        summary = collect_summary()
    except psycopg2.Error as exc:
        print(f"DB error: {exc}", file=sys.stderr)
        return 1

    message = format_message(summary)
    result = send_telegram(token, chat_id, message)
    if not result.get("ok"):
        print(f"Telegram send failed: {result}", file=sys.stderr)
        return 3
    msg_id = result.get("result", {}).get("message_id", "?")
    print(f"Sent {len(message)}-char summary to chat {chat_id} (msg_id={msg_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
