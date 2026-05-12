#!/usr/bin/env python3
"""Deterministic Telegram delivery for the devops-report-pipeline.

Reads /tmp/devops_report.json, formats it as a plain-text weekly report
using a fixed template (no LLM), and sends it directly to the Robothor
Telegram chat via the Bot API.

Required env:
  ROBOTHOR_TELEGRAM_BOT_TOKEN  — bot token
  ROBOTHOR_TELEGRAM_CHAT_ID    — target chat (Philip's DM by default)

Called by the workflow as step 6 (post-telegram). Exits non-zero on any
failure; the workflow's on_failure: abort kicks in.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IN_PATH = Path("/tmp/devops_report.json")
TELEGRAM_MAX_CHARS = 4000  # Leave slack under 4096 limit


def _pct(current: float, last: float) -> str:
    if not last:
        return ""
    delta = (current - last) / last * 100
    return f"{delta:+.0f}%"


def format_report(data: dict) -> str:
    """Format the structured JSON as a plain-text Telegram message."""
    es = data.get("executive_summary", {})
    cw = es.get("current_week", {})
    lw = es.get("last_week", {})

    lines: list[str] = []
    lines.append(f"Dev Team Ops Report — {data.get('period', 'this week')}")
    lines.append("")

    # Key numbers
    tickets_cw = cw.get("tickets_resolved", 0)
    tickets_lw = lw.get("tickets_resolved", 0)
    prs_cw = cw.get("prs_merged", 0)
    prs_lw = lw.get("prs_merged", 0)
    lines.append(
        f"▸ Tickets resolved: {tickets_cw} (vs {tickets_lw} last week, {_pct(tickets_cw, tickets_lw)})"
    )
    lines.append(f"▸ PRs merged: {prs_cw} (vs {prs_lw} last week, {_pct(prs_cw, prs_lw)})")

    review_cov = data.get("github", {}).get("review_coverage")
    total_reviews = data.get("github", {}).get("total_reviews")
    if review_cov is not None:
        flag = " ⚠" if review_cov < 30 else ""
        lines.append(f"▸ Review coverage: {review_cov}%{flag} ({total_reviews} reviews)")
    if es.get("open_backlog"):
        lines.append(f"▸ Open backlog: {es['open_backlog']}")

    # Bottlenecks (show high severity first, up to 5)
    bottlenecks = sorted(
        data.get("bottlenecks", []),
        key=lambda b: 0 if b.get("severity") == "high" else 1,
    )[:5]
    if bottlenecks:
        lines.append("")
        lines.append("⚠ What needs attention")
        for b in bottlenecks:
            text = b.get("text", "")
            rec = b.get("recommendation", "")
            lines.append(f"  ● {text}")
            if rec:
                lines.append(f"      → {rec}")

    # Personnel highlights — curate to the 4-5 most important reads
    analysis = data.get("personnel_analysis", [])
    # Prioritize: those with concerns > those without; keep first 5
    with_concerns = [p for p in analysis if p.get("concerns")]
    without = [p for p in analysis if not p.get("concerns")]
    highlights = (with_concerns + without)[:5]
    if highlights:
        lines.append("")
        lines.append("● Personnel highlights")
        for p in highlights:
            name = p.get("name", "?")
            snap_bits = []
            if p.get("concerns"):
                snap_bits.append(p["concerns"])
            elif p.get("strengths"):
                snap_bits.append(p["strengths"])
            if p.get("coaching"):
                snap_bits.append(f"→ {p['coaching']}")
            summary = " ".join(snap_bits) if snap_bits else p.get("snapshot", "")
            lines.append(f"  ● {name}: {summary}")

    # Top stale items
    stale = data.get("jira", {}).get("stale_tickets", [])[:3]
    if stale:
        lines.append("")
        lines.append("▸ Top stale JIRA")
        lines.extend(
            f"  ● {t.get('key', '?')} ({t.get('assignee', '?')}): {t.get('summary', '')}"
            for t in stale
        )

    lines.append("")
    lines.append(
        "Full HTML report emailed to stakeholders. Full data in devops_latest_report memory block — ask me to unpack any section."
    )

    message = "\n".join(lines)
    if len(message) > TELEGRAM_MAX_CHARS:
        # Trim from the middle of personnel highlights if needed
        message = message[: TELEGRAM_MAX_CHARS - 20] + "\n\n[truncated]"
    return message


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
    if not IN_PATH.exists():
        print(f"Input missing: {IN_PATH}", file=sys.stderr)
        return 1

    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "").strip()
    if not token:
        print("ROBOTHOR_TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 2
    if not chat_id:
        print("ROBOTHOR_TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    try:
        data = json.loads(IN_PATH.read_text())
    except json.JSONDecodeError as e:
        print(f"Report JSON invalid: {e}", file=sys.stderr)
        return 1

    message = format_report(data)
    result = send_telegram(token, chat_id, message)

    if not result.get("ok"):
        print(f"Telegram send failed: {result}", file=sys.stderr)
        return 3

    msg_id = result.get("result", {}).get("message_id", "?")
    print(f"Sent {len(message)} char message to chat {chat_id} (msg_id={msg_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
