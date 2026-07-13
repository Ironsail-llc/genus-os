"""An alert that the database rejects is an alert nobody receives.

`notification_type='alert'` violates the `crm_agent_notifications` check
constraint — the allowed set is {task_assigned, review_requested,
review_approved, review_rejected, blocked, unblocked, agent_error, info,
custom, escalation}. The INSERT is refused.

Worse, `send_notification` swallows the failure (logs, returns None), so the
caller sees success. Two callers were affected:

  * `feature_flags.notify_guardrail_alert` — the alert rung of the
    observe→alert→enforce ladder, i.e. the thing that makes the middle rung
    real, notified nobody and reported True.
  * `buddy_auditor` — its "self-improvement pipeline paused" CRITICAL alert
    has never reached the operator.

Both are the same failure this whole hardening pass keeps finding: a control
that runs, reports success, and does nothing. These tests pin delivery.
"""

from __future__ import annotations

import robothor.crm.dal as dal
from robothor.engine.feature_flags import notify_guardrail_alert

# The DB's check constraint on crm_agent_notifications.notification_type.
VALID_TYPES = {
    "task_assigned",
    "review_requested",
    "review_approved",
    "review_rejected",
    "blocked",
    "unblocked",
    "agent_error",
    "info",
    "custom",
    "escalation",
}


def test_guardrail_alert_uses_a_type_the_database_accepts(monkeypatch):
    import robothor.engine.feature_flags as ff

    sent: list[dict] = []
    monkeypatch.setattr(dal, "send_notification", lambda **kw: sent.append(kw) or "id-1")
    monkeypatch.setattr(ff, "_post_telegram", lambda text: True)

    assert notify_guardrail_alert(guardrail_name="exec_allowlist", agent_id="a", reason="r")

    assert len(sent) == 1
    assert sent[0]["notification_type"] in VALID_TYPES, (
        f"notification_type={sent[0]['notification_type']!r} violates the "
        "crm_agent_notifications check constraint — the INSERT is refused and "
        "the operator is never told"
    )


def test_guardrail_alert_reports_failure_when_delivery_fails(monkeypatch):
    """send_notification returns None on failure — that must not read as success."""
    import robothor.engine.feature_flags as ff

    monkeypatch.setattr(dal, "send_notification", lambda **kw: None)
    # Never let a test reach the operator's real Telegram.
    monkeypatch.setattr(ff, "_post_telegram", lambda text: False)

    assert (
        notify_guardrail_alert(guardrail_name="exec_allowlist", agent_id="a", reason="r") is False
    ), (
        "notify_guardrail_alert reported success while the notification was "
        "dropped — an alert nobody receives is the failure this rung exists "
        "to prevent"
    )


def test_buddy_auditor_uses_a_valid_notification_type():
    """The pipeline-paused alert must actually reach the operator."""
    from pathlib import Path

    import robothor.engine.buddy_auditor as ba

    src = Path(ba.__file__).read_text()
    # crude but effective: the literal passed to send_notification
    for bad in ('notification_type="alert"', "notification_type='alert'"):
        assert bad not in src, (
            "buddy_auditor sends notification_type='alert', which the database "
            "rejects — its critical 'pipeline paused' alert has never been "
            "delivered"
        )
