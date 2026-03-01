#!/usr/bin/env python3
"""Monitor task coordination system — checks for agent-created tasks and validates cutover."""

import json
import os
from datetime import UTC, datetime

import psycopg2
from psycopg2.extras import RealDictCursor

LOG = "/tmp/task-coord-monitor.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def check():
    log("=" * 60)
    log(f"Task Coordination Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Check for agent-created tasks
    conn = psycopg2.connect(dbname="robothor_memory", user="philip")
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT created_by_agent, assigned_to_agent, priority, tags, title, status,
               created_at, resolved_at, resolution
        FROM crm_tasks
        WHERE deleted_at IS NULL AND created_by_agent IS NOT NULL
        ORDER BY created_at DESC LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()

    if rows:
        log(f"CHECK 6: PASS — {len(rows)} agent-created task(s) found!")
        for r in rows:
            log(
                f"  [{r['status']}] {r['created_by_agent']} -> {r['assigned_to_agent']}: {r['title']}"
            )
            log(f"         priority={r['priority']}, tags={r['tags']}")
            if r["resolved_at"]:
                log(f"         resolved: {r['resolution']}")

        # Check 7: Any resolved by responder?
        responder_resolved = [
            r for r in rows if r["assigned_to_agent"] == "email-responder" and r["status"] == "DONE"
        ]
        if responder_resolved:
            log(f"CHECK 7: PASS — {len(responder_resolved)} task(s) resolved by email-responder")
        else:
            log("CHECK 7: WAITING — no responder-resolved tasks yet")

        # Check 8: Any escalation tasks for main?
        main_tasks = [r for r in rows if r["assigned_to_agent"] == "main"]
        if main_tasks:
            log(f"CHECK 8: PASS — {len(main_tasks)} escalation task(s) for main")
        else:
            log("CHECK 8: WAITING — no main escalation tasks yet")
    else:
        log("CHECK 6: WAITING — no agent-created tasks yet")
        log("CHECK 7: WAITING — depends on check 6")
        log("CHECK 8: WAITING — depends on check 6")

    # 2. Check triage_cleanup safety net (check 9)
    rq_path = os.path.expanduser("~/clawd/memory/response-queue.json")
    if os.path.exists(rq_path):
        mtime = os.path.getmtime(rq_path)
        mtime_dt = datetime.fromtimestamp(mtime)
        log(f"CHECK 9: response-queue.json last modified: {mtime_dt.strftime('%H:%M:%S')}")
        try:
            with open(rq_path) as f:
                data = json.load(f)
            items = data.get("items", [])
            log(f"  {len(items)} item(s) in queue")
        except Exception as e:
            log(f"  Error reading: {e}")

    # 3. Check worker-handoff.json not modified by agents (check 10)
    wh_path = os.path.expanduser("~/clawd/memory/worker-handoff.json")
    if os.path.exists(wh_path):
        mtime = os.path.getmtime(wh_path)
        mtime_dt = datetime.fromtimestamp(mtime)
        log(f"CHECK 10: worker-handoff.json last modified: {mtime_dt.strftime('%H:%M:%S')}")
        try:
            with open(wh_path) as f:
                data = json.load(f)
            escalations = data.get("escalations", [])
            # Check if any recent escalations were created by agents (not infra scripts)
            agent_sources = {"email", "calendar", "crm-steward"}
            recent_agent = []
            now = datetime.now(UTC)
            for e in escalations:
                created = e.get("createdAt", "")
                source = e.get("source", "")
                if source in agent_sources and created:
                    try:
                        ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_min = (now - ct).total_seconds() / 60
                        if age_min < 30:  # Created in last 30 min
                            recent_agent.append(e)
                    except Exception:
                        pass
            if recent_agent:
                log(
                    f"  WARNING: {len(recent_agent)} recent agent-written escalation(s) — cutover may not be complete"
                )
                for e in recent_agent:
                    log(f"    source={e.get('source')}, summary={e.get('summary', '')[:60]}")
            else:
                log("  OK — no recent agent-written escalations (infra scripts only)")
        except Exception as e:
            log(f"  Error reading: {e}")

    # 4. Agent last run times
    log("--- Agent Status Files ---")
    memory_dir = os.path.expanduser("~/clawd/memory")
    for fname in [
        "email-classifier-status.md",
        "calendar-monitor-status.md",
        "response-status.md",
        "vision-monitor-status.md",
    ]:
        path = os.path.join(memory_dir, fname)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if "last run" in line.lower():
                        log(f"  {fname}: {line.strip()}")
                        break

    log("=" * 60)
    log("")


if __name__ == "__main__":
    check()
