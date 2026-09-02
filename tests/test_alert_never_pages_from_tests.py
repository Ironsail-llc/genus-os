"""A test run must never page the operator.

2026-08-27: running the suite sent three real Telegram alerts to Philip's
phone, including "2 CORRUPT offsite (bytes differ from source):
robothor_memory-20260712.sql.gz" -- a fixture filename that reads exactly
like a genuine data-integrity emergency. Another named a pytest tmpdir
outright.

``tests/test_backup_offsite.py`` subprocess-runs the real
``scripts/backup-offsite.sh``, which calls the real
``scripts/send_failure_alert.sh``. The test passes a CLEAN env, so no pytest
marker reaches the subprocess -- but the pager re-sources credentials from
tmpfs itself, so it delivered anyway.

``model_breaker`` already guards this class in Python (``_in_pytest()``,
added after 92 of 145 production escalation rows turned out to be pytest
fixture models). The shell path had no equivalent.

Guard the path every caller crosses, not the one today's caller uses.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGER = REPO / "scripts" / "send_failure_alert.sh"


def test_the_pager_refuses_alerts_that_name_a_test_path():
    src = PAGER.read_text()
    assert "pytest-of-" in src, (
        "send_failure_alert.sh has no guard against paging on a message that "
        "names a pytest temp directory, so any suite run can spam the operator "
        "with fixture failures that read like real emergencies"
    )


def test_the_backup_test_uses_the_stub_api_seam():
    """The script exposes ROBOTHOR_TELEGRAM_API_BASE for exactly this."""
    src = (REPO / "tests" / "test_backup_offsite.py").read_text()
    assert "ROBOTHOR_TELEGRAM_API_BASE" in src, (
        "the offsite test drives the real alert path without redirecting it; "
        "the script already supports a stub base URL"
    )


def test_no_test_invokes_the_pager_against_the_real_api():
    """Any test that runs a script which can page must neutralise the send."""
    offenders = []
    for path in (REPO / "tests").rglob("test_*.py"):
        text = path.read_text(errors="ignore")
        drives_pager = re.search(
            r"backup-offsite\.sh|send_failure_alert\.sh|cron-wrapper\.sh", text
        )
        if not drives_pager:
            continue
        neutralised = (
            "ROBOTHOR_TELEGRAM_API_BASE" in text
            or "ROBOTHOR_ALERT_SUPPRESS" in text
            or "ROBOTHOR_TELEGRAM_BOT_TOKEN" in text
        )
        if not neutralised:
            offenders.append(path.name)
    assert not offenders, (
        f"these tests drive a script that can page the operator without "
        f"redirecting or suppressing delivery: {sorted(offenders)}"
    )
