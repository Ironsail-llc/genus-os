#!/usr/bin/env python3
"""
Jira Sync Cron — Focused, narrow scope.

Purpose:
    - Fetch Philip's Jira tickets
    - Log findings to jira-log.json
    - Update tasks.json with ticket status
    - Note actions needed (new tickets, status changes)

Does NOT:
    - Announce to Telegram (heartbeat's job)
    - Synthesize across data sources (heartbeat's job)
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from jira import JIRA

# === Config ===
JIRA_SERVER = "https://ironsail.atlassian.net"
JIRA_EMAIL = "philip@ironsail.ai"
JIRA_TOKEN = os.environ.get(
    "JIRA_API_TOKEN",
    "ATATT3xFfGF0LQ7h0Fqs-fESxJ6szZaIynxoxTwd4KHgY_Kwr8zsuM6wQgqfrOn_RUBqOuK8E-qt8fPzHONv8Y2_Yt8OItu7COOEsDM6XiTpvzhi9y4ByhUc3X-VVZjMX_89NNxlAxnPqisHK_Ky_zz9Rw9DE_EMjGOjN-CXvR-MYKFV33_GRCc=723E9D3C",
)

JIRA_LOG = Path("/home/philip/robothor/brain/memory/jira-log.json")
TASKS_FILE = Path("/home/philip/robothor/brain/memory/tasks.json")

# JQL: Only Philip's tickets
JQL_PHILIP = "assignee = currentUser() AND status != Done ORDER BY priority DESC, updated DESC"

# CRM DAL
sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
import crm_dal

CRM_STATUS_MAP = {
    "To Do": "TODO",
    "In Progress": "IN_PROGRESS",
    "In Review": "IN_PROGRESS",
    "Testing": "IN_PROGRESS",
    "On Hold": "TODO",
    "Done": "DONE",
    "Closed": "DONE",
}

PRIORITY_MAP = {
    "Highest": "critical",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "Lowest": "low",
}

STATUS_MAP = {
    "Done": "completed",
    "Closed": "completed",
    "To Do": "pending",
    "In Progress": "in_progress",
    "Testing": "in_progress",
    "On Hold": "on_hold",
}


def load_json(path: Path, default: dict) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, data: dict):
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _sync_ticket_to_crm(ticket: dict, change_type: str, prev: dict | None) -> str | None:
    """Create or update a CRM task for a Jira ticket. Returns task ID or None."""
    title = f"{ticket['id']}: {ticket['summary']}"
    body = (
        f"Priority: {ticket['priorityMapped']}\n"
        f"Project: {ticket['project']}\n"
        f"Jira Status: {ticket['status']}\n"
        f"URL: {ticket['url']}"
    )
    crm_status = CRM_STATUS_MAP.get(ticket["status"], "TODO")
    due_at = f"{ticket['dueDate']}T00:00:00Z" if ticket.get("dueDate") else None

    existing_crm_id = (prev or {}).get("crmTaskId")

    if existing_crm_id and change_type != "new":
        ok = crm_dal.update_task(
            existing_crm_id, title=title, body=body, status=crm_status, due_at=due_at
        )
        if ok:
            print(f"  CRM: updated task {existing_crm_id} for {ticket['id']}")
        return existing_crm_id
    else:
        task_id = crm_dal.create_task(title, body, crm_status, due_at)
        if task_id:
            print(f"  CRM: created task {task_id} for {ticket['id']}")
            return task_id
        return None


def _mark_crm_task_done(crm_task_id: str):
    """Mark a CRM task as DONE."""
    ok = crm_dal.update_task(crm_task_id, status="DONE")
    if ok:
        print(f"  CRM: marked task {crm_task_id} as DONE")


def connect() -> JIRA:
    return JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_TOKEN))


def sync():
    now = datetime.now().isoformat()

    # Load existing logs
    jira_log = load_json(
        JIRA_LOG,
        {
            "lastSyncAt": None,
            "lastSyncStatus": None,
            "syncHistory": [],
            "activeTickets": {},
            "pendingActions": [],
        },
    )

    tasks_data = load_json(TASKS_FILE, {"tasks": [], "completedTasks": []})

    # Track this sync
    sync_record = {
        "timestamp": now,
        "ticketsFetched": 0,
        "added": 0,
        "updated": 0,
        "closed": 0,
        "changes": [],
        "errors": [],
    }

    try:
        jira = connect()
        issues = jira.search_issues(JQL_PHILIP, maxResults=50)
        sync_record["ticketsFetched"] = len(issues)

        current_ticket_ids = set()

        for issue in issues:
            ticket_id = issue.key
            current_ticket_ids.add(ticket_id)

            priority = issue.fields.priority.name if issue.fields.priority else "Medium"
            status = issue.fields.status.name
            summary = issue.fields.summary
            due = issue.fields.duedate
            url = f"{JIRA_SERVER}/browse/{ticket_id}"

            ticket_data = {
                "id": ticket_id,
                "summary": summary,
                "status": status,
                "statusMapped": STATUS_MAP.get(status, "pending"),
                "priority": priority,
                "priorityMapped": PRIORITY_MAP.get(priority, "medium"),
                "dueDate": due,
                "url": url,
                "lastSeen": now,
                "project": issue.fields.project.key,
            }

            # Check if this is new or changed
            prev = jira_log["activeTickets"].get(ticket_id)

            if not prev:
                # New ticket
                sync_record["added"] += 1
                sync_record["changes"].append(
                    {"type": "new", "ticket": ticket_id, "summary": summary, "priority": priority}
                )
                jira_log["pendingActions"].append(
                    {
                        "action": "review_new_ticket",
                        "ticket": ticket_id,
                        "summary": summary,
                        "addedAt": now,
                        "surfacedAt": None,
                    }
                )
                # Create CRM task
                crm_id = _sync_ticket_to_crm(ticket_data, "new", None)
                if crm_id:
                    ticket_data["crmTaskId"] = crm_id
            elif prev.get("status") != status:
                # Status changed
                sync_record["updated"] += 1
                sync_record["changes"].append(
                    {
                        "type": "status_change",
                        "ticket": ticket_id,
                        "from": prev.get("status"),
                        "to": status,
                    }
                )
                # Update CRM task status
                if prev.get("crmTaskId"):
                    _sync_ticket_to_crm(ticket_data, "status_change", prev)
            elif prev.get("priority") != priority:
                # Priority changed
                sync_record["updated"] += 1
                sync_record["changes"].append(
                    {
                        "type": "priority_change",
                        "ticket": ticket_id,
                        "from": prev.get("priority"),
                        "to": priority,
                    }
                )
                # Update CRM task body
                if prev.get("crmTaskId"):
                    _sync_ticket_to_crm(ticket_data, "priority_change", prev)

            # Preserve crmTaskId from previous entry
            if prev and prev.get("crmTaskId") and "crmTaskId" not in ticket_data:
                ticket_data["crmTaskId"] = prev["crmTaskId"]

            # Update active tickets
            jira_log["activeTickets"][ticket_id] = ticket_data

            # Update tasks.json
            task_entry = {
                "id": ticket_id,
                "description": summary,
                "source": f"jira:{issue.fields.project.key.lower()}",
                "sourceDetails": f"Jira ticket - {issue.fields.project.name}",
                "priority": PRIORITY_MAP.get(priority, "medium"),
                "createdAt": issue.fields.created,
                "dueAt": due,
                "status": STATUS_MAP.get(status, "pending"),
                "owner": "Philip",
                "jiraUrl": url,
                "jiraStatus": status,
                "lastSynced": now,
            }

            # Find and update or add
            found = False
            for i, t in enumerate(tasks_data["tasks"]):
                if t.get("id") == ticket_id:
                    task_entry["notes"] = t.get("notes", "")  # Preserve notes
                    tasks_data["tasks"][i] = task_entry
                    found = True
                    break

            if not found:
                tasks_data["tasks"].append(task_entry)

        # Check for tickets that disappeared (completed externally)
        for ticket_id in list(jira_log["activeTickets"].keys()):
            if ticket_id not in current_ticket_ids:
                sync_record["closed"] += 1
                sync_record["changes"].append(
                    {
                        "type": "completed",
                        "ticket": ticket_id,
                        "summary": jira_log["activeTickets"][ticket_id].get("summary"),
                    }
                )
                # Mark CRM task as DONE
                crm_tid = jira_log["activeTickets"][ticket_id].get("crmTaskId")
                if crm_tid:
                    _mark_crm_task_done(crm_tid)
                # Mark as completed in tasks
                for t in tasks_data["tasks"]:
                    if t.get("id") == ticket_id:
                        t["status"] = "completed"
                        t["completedAt"] = now
                        break
                # Remove from active
                del jira_log["activeTickets"][ticket_id]

        # Backfill: create CRM tasks for any activeTickets missing crmTaskId
        backfilled = 0
        for tid, tdata in jira_log["activeTickets"].items():
            if not tdata.get("crmTaskId"):
                crm_id = _sync_ticket_to_crm(tdata, "new", None)
                if crm_id:
                    tdata["crmTaskId"] = crm_id
                    backfilled += 1
        if backfilled:
            print(f"  CRM: backfilled {backfilled} tasks")

        sync_record["status"] = "success"

    except Exception as e:
        sync_record["status"] = "error"
        sync_record["errors"].append(str(e))

    # Update log
    jira_log["lastSyncAt"] = now
    jira_log["lastSyncStatus"] = sync_record["status"]
    jira_log["syncHistory"].insert(0, sync_record)
    jira_log["syncHistory"] = jira_log["syncHistory"][:50]  # Keep last 50

    # Clear surfaced pending actions
    jira_log["pendingActions"] = [
        a for a in jira_log["pendingActions"] if a.get("surfacedAt") is None
    ]

    # Save
    save_json(JIRA_LOG, jira_log)
    save_json(TASKS_FILE, tasks_data)

    # Print summary for cron log
    print(
        f"[{now}] Jira Sync: {sync_record['ticketsFetched']} fetched, "
        f"{sync_record['added']} new, {sync_record['updated']} updated, "
        f"{sync_record['closed']} closed, status={sync_record['status']}"
    )

    if sync_record["changes"]:
        for c in sync_record["changes"]:
            print(f"  → {c['type']}: {c.get('ticket')} {c.get('summary', '')[:40]}")


if __name__ == "__main__":
    sync()
