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


# The scripts that reach the sender, directly or through one hop.
PAGER_SCRIPTS = (
    r"backup-offsite\.sh|send_failure_alert\.sh|cron-wrapper\.sh|liveness_probe\.sh"
)

# What a test that can reach the sender must pin. All three are real, shared,
# durable paths on a live box:
#
#   ROBOTHOR_ALERT_SPOOL_DIR   /var/lib/robothor/alert-spool — a page left here
#       is DELIVERED by the next 5-minute liveness drain. Not a suppressed
#       page: an actual message on the operator's phone, minutes after the
#       suite that wrote it exited.
#   ROBOTHOR_ALERT_STATE_DIR   /run/robothor/alert-cooldown — a stamp here
#       suppresses a REAL page for an hour.
#   ROBOTHOR_ALERT_FALLBACK_STATE_DIR  /tmp/robothor-alert-cooldown-<uid> —
#       where the cooldown lands when the primary is not writable, which is
#       every cron-driven page on this box. Same consequence, different path.
REQUIRED_PINS = (
    "ROBOTHOR_ALERT_SPOOL_DIR",
    "ROBOTHOR_ALERT_STATE_DIR",
    "ROBOTHOR_ALERT_FALLBACK_STATE_DIR",
)


def tests_that_can_reach_the_pager() -> list[tuple[Path, str]]:
    """Test files that RUN something able to page — not ones that merely read
    a script's source (tests/test_cold_boot.py asserts on the sender's text
    and executes nothing)."""
    found = []
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        text = path.read_text(errors="ignore")
        if "subprocess" not in text:
            continue
        if not re.search(PAGER_SCRIPTS, text) and "ROBOTHOR_TELEGRAM_BOT_TOKEN" not in text:
            continue
        found.append((path, text))
    return found


def test_every_test_that_can_page_pins_the_spool_and_the_state_dirs():
    """Redirecting the API is not enough any more.

    ROBOTHOR_TELEGRAM_API_BASE stops the send, but an undeliverable page is
    no longer dropped — it is SPOOLED, and the real spool is drained every 5
    minutes by root's liveness tick. So a test that neutralises delivery and
    forgets the spool does not avoid paging the operator; it delays it. The
    2026-08-27 accident with a longer fuse.
    """
    offenders = {}
    for path, text in tests_that_can_reach_the_pager():
        missing = [pin for pin in REQUIRED_PINS if pin not in text]
        if missing:
            offenders[path.name] = missing
    assert not offenders, (
        "these tests can run the pager without pinning the durable state it "
        f"writes — a page spooled by the suite is delivered for real by the "
        f"next liveness drain: {offenders}"
    )


def test_the_ratchet_actually_finds_the_files_it_is_meant_to_guard():
    """A ratchet whose scan matches nothing passes forever. Name the files."""
    names = {p.name for p, _ in tests_that_can_reach_the_pager()}
    for expected in (
        "test_pager_hardening.py",
        "test_failure_alerts.py",
        "test_liveness_watchdog.py",
        "test_backup_offsite.py",
    ):
        assert expected in names, f"the scan no longer sees {expected}"


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
