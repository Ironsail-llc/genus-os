#!/usr/bin/env python3
"""Deterministic email fan-out for the devops-report-pipeline.

Reads /tmp/devops_report.html + /tmp/devops_report_meta.json, fetches the
recipient list from the devops_report_recipients memory block, and sends
one HTML email per recipient via the gws CLI.

No LLM needed. Called by the workflow as step 5 (dispatch-emails). Exits
non-zero on total failure; continues on per-recipient failures and logs
the summary so the operator can see who got what.

Required env:
  ROBOTHOR_DEFAULT_TENANT — defaults to "robothor-primary"
  Postgres connection env (inherited from daemon; peer-auth to robothor_memory)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HTML_PATH = Path("/tmp/devops_report.html")
META_PATH = Path("/tmp/devops_report_meta.json")
TENANT = os.environ.get("ROBOTHOR_DEFAULT_TENANT", "robothor-primary")


def _load_recipients() -> list[dict]:
    """Read devops_report_recipients memory block from DB."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT content FROM agent_memory_blocks
               WHERE block_name = 'devops_report_recipients' AND tenant_id = %s""",
            (TENANT,),
        )
        row = cur.fetchone()
    if not row:
        return []
    content = row[0] if not isinstance(row, dict) else row["content"]
    return list(json.loads(content))


def _lookup_email(person_id: str) -> str | None:
    """Return the primary email for a CRM person_id, or None."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT email FROM crm_people
               WHERE id = %s AND deleted_at IS NULL""",
            (person_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    email = row[0] if not isinstance(row, dict) else row["email"]
    return email or None


def _send_html(to: str, subject: str, html_body: str) -> dict:
    """Send one HTML email via gws CLI. Returns gws API response."""
    msg = MIMEText(html_body, _subtype="html")
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    proc = subprocess.run(
        [
            "gws",
            "gmail",
            "users",
            "messages",
            "send",
            "--params",
            '{"userId":"me"}',
            "--json",
            json.dumps({"raw": raw}),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gws exit {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout or "{}")


def main() -> int:
    if not HTML_PATH.exists() or not META_PATH.exists():
        print(
            f"Render outputs missing (HTML={HTML_PATH.exists()}, "
            f"META={META_PATH.exists()}). Render step likely failed.",
            file=sys.stderr,
        )
        return 1

    html_body = HTML_PATH.read_text()
    meta = json.loads(META_PATH.read_text())
    subject = meta.get("subject") or "Dev Team Operations Report"

    recipients = _load_recipients()
    if not recipients:
        print(
            "No recipients configured in devops_report_recipients memory block.",
            file=sys.stderr,
        )
        return 1

    sent: list[str] = []
    failed: list[tuple[str, str]] = []

    for rec in recipients:
        name = rec.get("name", "<unknown>")
        pid = rec.get("person_id")
        if not pid:
            failed.append((name, "no person_id in recipient entry"))
            continue
        email = _lookup_email(pid)
        if not email:
            failed.append((name, f"no email in crm_people (id={pid})"))
            continue
        try:
            _send_html(email, subject, html_body)
            sent.append(f"{name} <{email}>")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))

    total = len(recipients)
    print(f"Emailed {len(sent)} of {total} recipients.")
    print(f"Subject: {subject}")
    print()
    if sent:
        print("Delivered:")
        for s in sent:
            print(f"  - {s}")
    if failed:
        print()
        print("Failed:")
        for name, err in failed:
            print(f"  - {name}: {err}")

    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
