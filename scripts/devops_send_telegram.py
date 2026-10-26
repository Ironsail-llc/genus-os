#!/usr/bin/env python3
"""Post a short devops report digest to Telegram (deterministic, no LLM).

Reads ``report.json`` (the rendered context written by
devops_render_report.py) from ``$DEVOPS_RUNS_DIR/<week-start>/``. Every number
comes from the aggregate's team totals; a metric that was unavailable prints
as ``n/a``. Robust to either a list or a count wherever a collection is
expected — an earlier analyst-shaped JSON turned lists into ints and crashed
this step every week.

Required env: ROBOTHOR_TELEGRAM_BOT_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.devops_periods import period_label, report_window, window_block
from robothor.engine.reports.devops_team import runs_dir, week_run_dir

TELEGRAM_MAX_CHARS = 4000  # Leave slack under the 4096 limit


def _count(v: Any) -> int:
    """A collection's size or an int as given; anything else counts as 0."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, (list, dict, tuple, set)):
        return len(v)
    if isinstance(v, str):
        return 1 if v.strip() else 0
    return 0


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def format_report(ctx: dict) -> str:
    team = ctx.get("team") if isinstance(ctx.get("team"), dict) else {}
    lines = [f"Dev Team Ops Report — {period_label(ctx.get('period'))}", ""]
    for label, key in (
        ("PRs merged", "prs_merged"),
        ("Commits", "commits"),
        ("Tickets resolved", "tickets_resolved"),
        ("Reviews given", "reviews_given"),
        ("Merged without review", "merged_without_review"),
    ):
        lines.append(f"▸ {label}: {_fmt(team.get(key))}")
    if ctx.get("open_backlog") is not None:
        lines.append(f"▸ Open backlog: {ctx['open_backlog']}")

    narrative = ctx.get("narrative") if isinstance(ctx.get("narrative"), dict) else {}
    if isinstance(narrative.get("summary"), str) and narrative["summary"].strip():
        lines += ["", narrative["summary"].strip()]
    bottlenecks = narrative.get("bottlenecks")
    items = []
    for b in bottlenecks if isinstance(bottlenecks, list) else []:
        if isinstance(b, str):
            items.append((1, b))
        elif isinstance(b, dict):
            text = b.get("text") or b.get("description") or ""
            if text:
                items.append((0 if b.get("severity") == "high" else 1, str(text)))
    if items:
        lines += ["", "⚠ What needs attention"]
        lines += [f"  ● {t}" for _, t in sorted(items, key=lambda x: x[0])[:5]]

    dq = ctx.get("data_quality") if isinstance(ctx.get("data_quality"), dict) else {}
    flags = []
    for key, text in (
        ("unavailable_metrics", "metric(s) unavailable"),
        ("outside_roster", "active handle(s) not on the roster"),
        ("no_activity", "roster member(s) with no activity"),
        ("narrative_withheld", "note(s) withheld — numbers didn't match"),
    ):
        n = _count(dq.get(key))
        if n:
            flags.append(f"{n} {text}")
    if dq.get("usage_unavailable"):
        flags.append("Claude usage unavailable")
    if flags:
        lines += ["", "⚠ Report quality: " + "; ".join(flags) + " (see email)"]

    lines += ["", "Full HTML report emailed to stakeholders."]
    message = "\n".join(lines)
    if len(message) > TELEGRAM_MAX_CHARS:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    in_path = week_run_dir(period["week_start"], args.runs_dir or runs_dir()) / "report.json"
    if not in_path.exists():
        print(f"Input missing: {in_path}", file=sys.stderr)
        return 1

    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ROBOTHOR_TELEGRAM_BOT_TOKEN / ROBOTHOR_TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    try:
        ctx = json.loads(in_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Report JSON invalid: {e}", file=sys.stderr)
        return 1

    message = format_report(ctx if isinstance(ctx, dict) else {})
    result = send_telegram(token, chat_id, message)
    if not result.get("ok"):
        print(f"Telegram send failed: {result}", file=sys.stderr)
        return 3
    print(f"Sent {len(message)} char digest to chat {chat_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
