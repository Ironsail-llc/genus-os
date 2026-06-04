#!/usr/bin/env python3
"""Fetch + parse the weekly Claude Usage Report and archive it locally.

The 'Claude Usage Interceptor · IronSail' automation emails a weekly
plaintext report from `support@impetusrx.com` to `robothor@ironsail.ai`.
This script:

  1. Pulls the most recent matching email via `gog gmail`.
  2. Parses the body into a structured JSON record.
  3. Writes both raw text + structured JSON into
     `local/devops/claude_usage_reports/` keyed by report period.
  4. Writes `/tmp/devops_claude_usage.json` for the workflow pipeline.

Designed to be a thin CLI; `parse_report(body) -> dict` is importable
and unit-tested without any network/email dependency.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "local" / "devops" / "claude_usage_reports"
OUT_LATEST = Path("/tmp/devops_claude_usage.json")

REPORT_SUBJECT_HINT = "Claude Usage Report"
REPORT_SENDER_HINT = "support@impetusrx.com"


_EMAIL_RE = re.compile(r"^[A-Za-z][\w.\-]*@[\w.\-]+\.[A-Za-z]{2,}$")
_INT_RE = re.compile(r"^\d+$")
_PCT_RE = re.compile(r"^(\d+)%$")
_FLOAT_RE = re.compile(r"^\d+\.\d+$")
_NEAR_LIMIT_RE = re.compile(r"^(\d+)×?$")


def _strip_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_report(body: str) -> dict[str, Any]:
    """Parse the plaintext usage report body into a structured record."""
    lines = _strip_lines(body)

    result: dict[str, Any] = {
        "period": "",
        "generated_at": "",
        "total_users": None,
        "missing_users": None,
        "near_limit_events": None,
        "extra_usage_cost_usd": None,
        "top_user": "",
        "highest_peak_util_pct": None,
        "users": [],
    }

    # Header section (sequential)
    for i, ln in enumerate(lines):
        if ln.startswith("Week of"):
            result["period"] = ln
        elif ln.startswith("Generated "):
            result["generated_at"] = ln[len("Generated ") :].strip()
        elif ln == "Total users" and i + 1 < len(lines):
            with contextlib.suppress(ValueError):
                result["total_users"] = int(lines[i + 1])
        elif "missing this week" in ln:
            m = re.match(r"^(\d+) missing this week", ln)
            if m:
                result["missing_users"] = int(m.group(1))
        elif ln == "Near-limit events" and i + 1 < len(lines):
            with contextlib.suppress(ValueError):
                result["near_limit_events"] = int(lines[i + 1])
        elif ln == "Extra usage cost" and i + 1 < len(lines):
            with contextlib.suppress(ValueError):
                result["extra_usage_cost_usd"] = float(lines[i + 1])
        elif ln.startswith("Top: "):
            result["top_user"] = ln[len("Top: ") :].strip()
        elif ln == "Highest peak util" and i + 1 < len(lines):
            m = _PCT_RE.match(lines[i + 1])
            if m:
                result["highest_peak_util_pct"] = int(m.group(1))

    # User rows: every line that's a bare email address starts a record.
    # Two shapes:
    #   active (multi-line): email / avgPct / peakPct / trend / nearLimit× / days / latest / cost / errors
    #   missing (single line): "user@example.com — — — — No data — — —"
    users: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        # Inline "missing" row — email followed by em-dashes on same line
        if " — " in ln and " No data " in ln:
            parts = ln.split(" ", 1)
            email = parts[0]
            if _EMAIL_RE.match(email):
                users.append(
                    {
                        "email": email,
                        "has_data": False,
                        "avg_util_pct": None,
                        "peak_util_pct": None,
                        "week_trend_pct": None,
                        "near_limit_events": 0,
                        "days_with_data": 0,
                        "latest_activity": "",
                        "extra_usage_cost_usd": 0.0,
                        "errors": 0,
                    }
                )
            i += 1
            continue

        if _EMAIL_RE.match(ln) and i + 2 < len(lines):
            # Active row: next 2 lines are avg% and peak%
            avg_m = _PCT_RE.match(lines[i + 1])
            peak_m = _PCT_RE.match(lines[i + 2])
            if avg_m and peak_m:
                # The 4th physical line aggregates: "0% 1× 1 / 7 May 19, ... 0.00 0"
                metrics_line = lines[i + 3] if i + 3 < len(lines) else ""
                # Split by whitespace, then re-piece "latest activity" which contains spaces
                parts = metrics_line.split()
                # parts: [trend%, nearLimit, '1', '/', '7', 'May', '19,', '07:03', 'PM', 'UTC', '0.00', '0']
                trend_pct = None
                near_limit = 0
                days_with_data = 0
                latest = ""
                cost = 0.0
                errors = 0
                if parts:
                    # trend
                    m = _PCT_RE.match(parts[0])
                    if m:
                        trend_pct = int(m.group(1))
                    # near-limit events: either "1×" or "0"
                    if len(parts) > 1:
                        m = _NEAR_LIMIT_RE.match(parts[1])
                        if m:
                            near_limit = int(m.group(1))
                        elif _INT_RE.match(parts[1]):
                            near_limit = int(parts[1])
                    # days "1 / 7"
                    if len(parts) > 4 and parts[3] == "/":
                        with contextlib.suppress(ValueError):
                            days_with_data = int(parts[2])
                    # latest activity is between the "/ 7" and the trailing cost+errors
                    # Last two tokens are cost (float) and errors (int)
                    if len(parts) >= 2:
                        with contextlib.suppress(ValueError, IndexError):
                            errors = int(parts[-1])
                            cost = float(parts[-2])
                            latest_tokens = parts[5:-2] if len(parts) > 7 else []
                            latest = " ".join(latest_tokens)

                users.append(
                    {
                        "email": ln,
                        "has_data": True,
                        "avg_util_pct": int(avg_m.group(1)),
                        "peak_util_pct": int(peak_m.group(1)),
                        "week_trend_pct": trend_pct,
                        "near_limit_events": near_limit,
                        "days_with_data": days_with_data,
                        "latest_activity": latest,
                        "extra_usage_cost_usd": cost,
                        "errors": errors,
                    }
                )
                i += 4
                continue
        i += 1

    result["users"] = users
    return result


def _run_gog(args: list[str]) -> dict[str, Any]:
    cp = subprocess.run(
        ["gog", *args, "--account", "robothor@ironsail.ai", "--json", "--no-input"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if cp.returncode != 0:
        return {"error": cp.stderr.strip() or cp.stdout.strip()[:500]}
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"gog returned non-JSON: {e}; stdout={cp.stdout[:200]}"}


_REPORT_BODY_MARKERS = ("Week of", "User breakdown")


def _looks_like_report_body(body: str) -> bool:
    """A real usage-report body must contain both markers.

    Skips Drive share notifications, calendar invites, and other emails
    that happen to have 'Claude Usage Report' in the subject line.
    """
    return all(marker in body for marker in _REPORT_BODY_MARKERS)


def _extract_text_body(payload: dict) -> str:
    """Recursively walk MIME parts and concatenate text/plain bodies."""
    chunks: list[str] = []

    def _walk(part: dict) -> None:
        mt = part.get("mimeType", "")
        if mt == "text/plain":
            data = (part.get("body") or {}).get("data", "")
            if data:
                chunks.append(
                    base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                )
        for sub in part.get("parts", []) or []:
            _walk(sub)

    _walk(payload)
    return "\n".join(chunks)


def fetch_most_recent_report() -> dict[str, Any] | None:
    """Find the latest *actual* Claude Usage Report and return the parsed body.

    Filters out Drive share notifications + other Subject-line collisions
    by requiring the body to contain the report markers.

    Returns None if no such email exists in robothor@ironsail.ai. Returns
    a dict with `{"parsed": ..., "raw_body": ..., ...}` on success.
    """
    search = _run_gog(
        [
            "gmail",
            "messages",
            "search",
            f'subject:"{REPORT_SUBJECT_HINT}" newer_than:30d',
        ]
    )
    if "error" in search:
        return {"error": search["error"]}
    msgs = search.get("messages") or []
    if not msgs:
        return None

    # Sort newest-first, walk until we find one with a real report body
    msgs_sorted = sorted(msgs, key=lambda m: m.get("date") or "", reverse=True)

    for top in msgs_sorted:
        thread_id = top.get("threadId") or top.get("id")
        thread = _run_gog(["gmail", "thread", "get", thread_id])
        if "error" in thread:
            continue

        msgs_in = (thread.get("thread") or {}).get("messages") or thread.get("messages", [])
        # Walk all messages in the thread (newest first) for a real body
        for msg in reversed(msgs_in):
            payload = msg.get("payload") or {}
            headers = {h.get("name", ""): h.get("value", "") for h in payload.get("headers", [])}
            body_text = _extract_text_body(payload)
            if not _looks_like_report_body(body_text):
                continue
            parsed = parse_report(body_text)
            return {
                "parsed": parsed,
                "raw_body": body_text,
                "received_at": headers.get("Date", ""),
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "message_id": msg.get("id", ""),
            }

    return None


def _archive(report: dict[str, Any]) -> Path:
    """Save the report into the local archive keyed by period slug."""
    period = report["parsed"].get("period") or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", period.lower()).strip("-")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_json = ARCHIVE_DIR / f"{slug}.json"
    out_raw = ARCHIVE_DIR / f"{slug}.txt"
    out_json.write_text(
        json.dumps(
            {
                "subject": report.get("subject"),
                "from": report.get("from"),
                "received_at": report.get("received_at"),
                "message_id": report.get("message_id"),
                "parsed": report["parsed"],
            },
            indent=2,
        )
    )
    out_raw.write_text(report.get("raw_body") or "")
    return out_json


def main() -> int:
    report = fetch_most_recent_report()
    if report is None:
        print("No Claude Usage Report found in last 14 days.", file=sys.stderr)
        # Still write a stub so downstream steps can read a stable file
        OUT_LATEST.write_text(json.dumps({"available": False}))
        return 0
    if "error" in report:
        print(f"Fetch error: {report['error']}", file=sys.stderr)
        return 1

    archive_path = _archive(report)
    payload = {
        "available": True,
        "archive_path": str(archive_path),
        **report["parsed"],
    }
    OUT_LATEST.write_text(json.dumps(payload, indent=2))
    p = report["parsed"]
    print(
        f"Claude usage: period={p.get('period')!r}; "
        f"{p.get('total_users')} users, "
        f"{p.get('missing_users')} missing, "
        f"top={p.get('top_user')} ({p.get('highest_peak_util_pct')}%)"
    )
    print(f"Archived to {archive_path}")
    print(f"Pipeline payload at {OUT_LATEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
