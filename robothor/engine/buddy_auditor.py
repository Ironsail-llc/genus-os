"""Buddy Auditor — weekly hold-rate guardrail.

Reads the verifications that `buddy-grader` produced over the past 14 days.
For every `verified_resolved` task that got a `held_7d=true|false` tag,
computes the hold-rate. If under the threshold (default 30%), pauses Buddy's
cron and emits a critical Telegram notification. The operator re-enables
manually after diagnosing.

This is the falsifiability clause: if the self-improvement pipeline isn't
producing durable fixes, the system concludes that itself and stops.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "brain" / "journals" / "buddy"
BUDDY_MANIFEST = Path(__file__).resolve().parent.parent.parent / "docs" / "agents" / "buddy.yaml"

HOLD_RATE_WINDOW_DAYS = 14
HOLD_RATE_THRESHOLD = 0.30
MIN_SAMPLES = 5  # Below this many verifications, don't judge the loop yet


@dataclass
class AuditOutcome:
    window_start: datetime
    window_end: datetime
    total_verifications: int
    held_true: int
    held_false: int
    hold_rate: float | None
    threshold: float
    action: str  # "ok" | "insufficient_samples" | "paused"
    message: str


def compute_hold_rate(
    *,
    window_days: int = HOLD_RATE_WINDOW_DAYS,
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[int, int, int]:
    """Return (held_true, held_false, total_verifications) over the window.

    Reads crm_tasks tagged `verified_resolved` whose resolved/updated timestamp
    falls within the window. Tasks missing a `held_7d=*` tag are still in the
    grace period — they count toward `total_verifications` but not the rate
    numerator/denominator.
    """
    from robothor.crm.dal import list_tasks

    start = datetime.now(UTC) - timedelta(days=window_days)
    held_true = 0
    held_false = 0
    total = 0
    for task in list_tasks(
        tags=["verified_resolved"],
        limit=500,
        exclude_resolved=False,
        tenant_id=tenant_id,
    ):
        updated = task.get("updated_at")
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < start:
                continue
        total += 1
        tags = task.get("tags") or []
        if "held_7d=true" in tags:
            held_true += 1
        elif "held_7d=false" in tags:
            held_false += 1
    return held_true, held_false, total


def pause_buddy_manifest() -> bool:
    """Comment out the cron line in buddy.yaml so the scheduler stops firing.

    Returns True if the pause was applied, False if already paused.
    Restoring the cron requires manual edit (documented in BUDDY.md).
    """
    if not BUDDY_MANIFEST.is_file():
        logger.warning("Buddy manifest not found at %s — cannot pause", BUDDY_MANIFEST)
        return False
    content = BUDDY_MANIFEST.read_text()
    # Look for the active cron line; skip if already commented out.
    marker = "# AUTO-PAUSED by buddy-auditor"
    if marker in content:
        return False
    out_lines: list[str] = []
    paused = False
    for line in content.splitlines():
        stripped = line.strip()
        if (
            not paused
            and stripped.startswith("cron:")
            and "6-22" in stripped
            and not stripped.startswith("#")
        ):
            out_lines.append(f"  # {marker} {datetime.now(UTC).isoformat()}")
            out_lines.append(f"  # {line.strip()}")
            out_lines.append('  cron: ""  # paused — edit this line to re-enable')
            paused = True
        else:
            out_lines.append(line)
    if not paused:
        return False
    BUDDY_MANIFEST.write_text("\n".join(out_lines) + "\n")
    return True


def emit_critical_notification(message: str, *, tenant_id: str = DEFAULT_TENANT) -> None:
    """Drop a critical notification so the operator sees it on next heartbeat.

    Uses the agent-to-agent notification surface (`send_notification`) addressed
    to `main`, with `notification_type='alert'`. Main's heartbeat surfaces
    unread notifications in the next delivery cycle — that's how the operator
    learns the pipeline auto-paused.
    """
    try:
        from robothor.crm.dal import send_notification

        send_notification(
            from_agent="buddy-auditor",
            to_agent="main",
            notification_type="alert",
            subject="Buddy self-improvement pipeline paused",
            body=message,
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.warning("Failed to send critical notification: %s", e)


def run_sentinel_check(hours: int = 168, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """Run the review-quality sentinel as part of the weekly audit.

    168h = 7 days so the sentinel rides the same window as the hold-rate check.
    Returns the sentinel report dict; alerts main directly if filler-rate is
    above the sentinel's threshold.
    """
    try:
        from brain.scripts.buddy_review_quality_sentinel import (
            audit_recent_reviews,
            emit_alert_if_drifting,
        )
    except Exception as e:
        logger.warning("Sentinel unavailable: %s", e)
        return {"error": str(e)}

    report = audit_recent_reviews(hours=hours, tenant_id=tenant_id)
    alerted = emit_alert_if_drifting(report)
    report["alerted"] = alerted
    return report


def run_audit(
    *,
    threshold: float = HOLD_RATE_THRESHOLD,
    window_days: int = HOLD_RATE_WINDOW_DAYS,
    min_samples: int = MIN_SAMPLES,
    tenant_id: str = DEFAULT_TENANT,
) -> AuditOutcome:
    """Weekly audit. Pauses Buddy if held-7d rate fell below threshold.

    Also runs the review-quality sentinel on the same window — that alert is
    separate and doesn't pause Buddy; it's a soft warning that the prompt may
    be drifting into filler territory.
    """
    # Sentinel is fire-and-alert — doesn't gate the hold-rate check.
    sentinel_report = run_sentinel_check(hours=window_days * 24, tenant_id=tenant_id)
    logger.info(
        "Buddy sentinel: %s reviews, %s%% filler",
        sentinel_report.get("total_reviews"),
        int((sentinel_report.get("filler_rate") or 0) * 100),
    )

    held_true, held_false, total = compute_hold_rate(window_days=window_days, tenant_id=tenant_id)

    # Only count tasks that have their hold-check complete in the rate.
    scored = held_true + held_false
    hold_rate = (held_true / scored) if scored > 0 else None

    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)

    if scored < min_samples:
        outcome = AuditOutcome(
            window_start=window_start,
            window_end=now,
            total_verifications=total,
            held_true=held_true,
            held_false=held_false,
            hold_rate=hold_rate,
            threshold=threshold,
            action="insufficient_samples",
            message=(
                f"Only {scored} hold-checks completed in the last {window_days} days "
                f"(need {min_samples}). Not pausing Buddy — giving the loop more time."
            ),
        )
    elif hold_rate is not None and hold_rate < threshold:
        paused = pause_buddy_manifest()
        action = "paused" if paused else "paused (already)"
        msg = (
            f"Self-improvement hold-rate is {hold_rate:.0%} over last {window_days} days "
            f"({held_true}/{scored} fixes stuck). Below threshold {threshold:.0%}. "
            f"Buddy's cron has been cleared — {'the manifest was edited' if paused else 'manifest was already paused'}. "
            f"Review brain/journals/buddy/ and /api/buddy/verifications before re-enabling."
        )
        outcome = AuditOutcome(
            window_start=window_start,
            window_end=now,
            total_verifications=total,
            held_true=held_true,
            held_false=held_false,
            hold_rate=hold_rate,
            threshold=threshold,
            action=action,
            message=msg,
        )
        emit_critical_notification(msg, tenant_id=tenant_id)
    else:
        outcome = AuditOutcome(
            window_start=window_start,
            window_end=now,
            total_verifications=total,
            held_true=held_true,
            held_false=held_false,
            hold_rate=hold_rate,
            threshold=threshold,
            action="ok",
            message=(
                f"Hold-rate {(hold_rate or 0):.0%} over last {window_days} days "
                f"({held_true}/{scored}). Above threshold — loop is producing durable fixes."
            ),
        )

    _journal(
        "audit",
        {
            "window_days": window_days,
            "total_verifications": outcome.total_verifications,
            "held_true": outcome.held_true,
            "held_false": outcome.held_false,
            "hold_rate": outcome.hold_rate,
            "threshold": outcome.threshold,
            "action": outcome.action,
            "message": outcome.message,
        },
    )
    return outcome


def _journal(event: str, payload: dict[str, Any]) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        path = JOURNAL_DIR / f"{today}.jsonl"
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **payload,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.debug("Auditor journal write failed (%s): %s", event, e)


# Expose subprocess so tests can monkeypatch if needed.
__all__ = [
    "AuditOutcome",
    "HOLD_RATE_THRESHOLD",
    "HOLD_RATE_WINDOW_DAYS",
    "MIN_SAMPLES",
    "compute_hold_rate",
    "emit_critical_notification",
    "pause_buddy_manifest",
    "run_audit",
    "subprocess",  # for test access
]
