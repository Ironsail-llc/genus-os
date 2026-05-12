#!/usr/bin/env python3
"""Deterministic render step for the devops-report-pipeline workflow.

Reads /tmp/devops_report.json (produced by the devops-analyst agent),
calls robothor.engine.reports.renderer.render_devops_report, and writes:

  /tmp/devops_report.html       — full HTML email body
  /tmp/devops_report_meta.json  — {subject, plain_summary, period}

The emailer agent reads both files and fans out to stakeholders.

Exits non-zero on any failure so the workflow's on_failure: abort kicks in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path so robothor.engine imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.renderer import render_devops_report

IN_PATH = Path("/tmp/devops_report.json")
HTML_OUT = Path("/tmp/devops_report.html")
META_OUT = Path("/tmp/devops_report_meta.json")

# Freshness window: analyst must have written the JSON within this many seconds.
# Prevents a pipeline with a crashed analyst from silently reusing a stale report.
MAX_AGE_SECONDS = 1800  # 30 minutes


def main() -> int:
    # Defensive: remove any stale outputs from prior runs before we validate.
    # This ensures downstream steps (emailer, reporter) can't read a stale file
    # if we fail and return non-zero — their read_file will error clearly.
    for p in (HTML_OUT, META_OUT):
        if p.exists():
            p.unlink()

    if not IN_PATH.exists():
        print(f"Input missing: {IN_PATH}", file=sys.stderr)
        return 1

    import time

    age = time.time() - IN_PATH.stat().st_mtime
    if age > MAX_AGE_SECONDS:
        print(
            f"Input stale: {IN_PATH} is {int(age)}s old (> {MAX_AGE_SECONDS}s)",
            file=sys.stderr,
        )
        return 1

    try:
        data = json.loads(IN_PATH.read_text())
    except json.JSONDecodeError as e:
        print(f"Input JSON invalid: {e}", file=sys.stderr)
        return 1

    required = [
        "period",
        "executive_summary",
        "jira",
        "github",
        "people",
        "bottlenecks",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        print(f"Report JSON missing required keys: {missing}", file=sys.stderr)
        return 1

    rendered = render_devops_report(data)
    if "error" in rendered:
        print(f"Render failed: {rendered['error']}", file=sys.stderr)
        return 1

    HTML_OUT.write_text(rendered["html"])
    META_OUT.write_text(
        json.dumps(
            {
                "subject": rendered["subject"],
                "plain_summary": rendered["plain_summary"],
                "period": data.get("period", ""),
            },
            indent=2,
        )
    )
    print(
        f"Rendered {len(rendered['html'])} char HTML → {HTML_OUT} (subject: {rendered['subject']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
