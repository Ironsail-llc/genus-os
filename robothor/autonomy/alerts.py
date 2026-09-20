"""Owner-facing money alerts for delegated payments.

Overspend that only colours a private panel is overspend nobody hears about.
These alerts go out on the platform's existing agent-to-agent notification
surface -- ``crm_agent_notifications`` via :func:`robothor.crm.dal.send_notification`,
the same row type the engine's guardrail and provider alerts already use, and
the one main's heartbeat surfaces to the operator. No new channel is invented
here, and nothing about a payment fact beyond its shape ever enters the body:
issuer references live only in the encrypted journal.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _in_pytest() -> bool:
    """Never page the operator from a test session.

    Mirrors ``provider_alerts._in_pytest``: the autonomy suite drives real
    overcharges against a disposable database, and every one of them would
    otherwise land in the operator's real inbox.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def notify_owner(*, tenant_id: str, subject: str, body: str) -> str | None:
    """Raise an owner escalation. Returns the notification id, or ``None``.

    Best effort by design: a money alert that cannot be delivered must not
    roll back the durable evidence that triggered it. A dropped alert is
    logged at error level, because an alert nobody receives is exactly the
    failure this module exists to prevent.
    """
    if _in_pytest():
        logger.info("autonomy money alert suppressed inside pytest: %s", subject)
        return None
    try:
        from robothor.crm import dal

        # "escalation" -- NOT "alert": the crm_agent_notifications check
        # constraint rejects "alert", and send_notification swallows the
        # refusal, so the returned id is the only proof of delivery.
        notification_id = dal.send_notification(
            from_agent="autonomy",
            to_agent="main",
            notification_type="escalation",
            subject=subject,
            body=body,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # pragma: no cover - delivery is best effort
        logger.error("autonomy money alert could not be raised (%s): %s", subject, exc)
        return None
    if not notification_id:
        logger.error("autonomy money alert was dropped, the owner has not been told: %s", subject)
    return notification_id
