#!/usr/bin/env python3
"""Email the rendered devops report — one message, every recipient in To:.

Reads ``report.html`` + ``meta.json`` from ``$DEVOPS_RUNS_DIR/<week-start>/``
and sends them via the gws CLI.

Recipients, first match wins:
  1. ``--to a@x,b@y``                 (override, e.g. for a dry run to yourself)
  2. ``DEVOPS_REPORT_RECIPIENTS``      (comma-separated email addresses)
  3. the ``devops_report_recipients`` memory block: a JSON list whose entries
     are an email string, ``{"email": ...}``, or ``{"name", "person_id"}``
     resolved through the CRM (crm_people.email, then contact_identifiers).

Exit 1 when any recipient cannot be resolved (the resolvable ones are still
sent the report, and the unresolved ones are listed), when there are no
recipients, or when the send fails.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.constants import DEFAULT_TENANT
from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_team import env_list, runs_dir, week_run_dir

TENANT = os.environ.get("ROBOTHOR_DEFAULT_TENANT", DEFAULT_TENANT)
_EMAIL = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[A-Za-z]{2,}$")


def _load_recipients() -> list:
    """The devops_report_recipients memory block, as a list."""
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
    data = json.loads(content)
    return list(data) if isinstance(data, list) else []


def _lookup_email(person_id: str) -> str | None:
    """A deliverable email for a CRM person: crm_people.email, then contact_identifiers."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT email FROM crm_people
               WHERE id = %s AND email IS NOT NULL
               ORDER BY deleted_at NULLS FIRST
               LIMIT 1""",
            (person_id,),
        )
        row = cur.fetchone()
        if row:
            email = row[0] if not isinstance(row, dict) else row["email"]
            if email:
                return email
        cur.execute(
            """SELECT identifier FROM contact_identifiers
               WHERE person_id = %s AND channel = 'email' AND identifier LIKE %s
               ORDER BY created_at DESC
               LIMIT 1""",
            (person_id, "%@%.%"),
        )
        row = cur.fetchone()
    if not row:
        return None
    ident = row[0] if not isinstance(row, dict) else row["identifier"]
    return ident or None


def _split(raw: str) -> list[str]:
    return [a.strip() for a in raw.split(",") if a.strip()]


def resolve_recipients(override: str | None) -> tuple[list[str], list[str]]:
    """(addresses, unresolved descriptions)."""
    entries: list = []
    if override:
        entries = _split(override)
    elif env_list("DEVOPS_REPORT_RECIPIENTS"):
        entries = env_list("DEVOPS_REPORT_RECIPIENTS")
    else:
        entries = _load_recipients()

    addresses: list[str] = []
    unresolved: list[str] = []
    for e in entries:
        if isinstance(e, str):
            label, email = e, e.strip()
        elif isinstance(e, dict):
            label = e.get("name") or e.get("email") or e.get("person_id") or "<unknown>"
            email = (e.get("email") or "").strip()
            if not email and e.get("person_id"):
                email = _lookup_email(str(e["person_id"])) or ""
        else:
            unresolved.append(f"{e!r}: unrecognised entry")
            continue
        if not _EMAIL.match(email):
            unresolved.append(f"{label}: no valid email address")
            continue
        if email.lower() not in {a.lower() for a in addresses}:
            addresses.append(email)
    return addresses, unresolved


def _send_html(to: list[str], subject: str, html_body: str) -> dict:
    """Send one HTML email to every address in ``to`` via gws."""
    msg = MIMEText(html_body, _subtype="html")
    msg["To"] = ", ".join(to)
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
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gws exit {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout or "{}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    ap.add_argument("--to", help="comma-separated addresses; overrides every other source")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    run = week_run_dir(period["week_start"], args.runs_dir or runs_dir())
    html_path, meta_path = run / "report.html", run / "meta.json"
    if not html_path.exists() or not meta_path.exists():
        print(f"render outputs missing in {run} — did the render step run?", file=sys.stderr)
        return 1
    subject = json.loads(meta_path.read_text()).get("subject") or "Dev Team Operations Report"

    addresses, unresolved = resolve_recipients(args.to)
    for u in unresolved:
        print(f"UNRESOLVED recipient — {u}", file=sys.stderr)
    if not addresses:
        print("no deliverable recipients", file=sys.stderr)
        return 1

    try:
        _send_html(addresses, subject, html_path.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"send failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"Sent '{subject}' to {len(addresses)} recipient(s): {', '.join(addresses)}")
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
