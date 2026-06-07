"""Forward audit events to a SIEM (Wave-2, W2-21 — enterprise audit).

Best-effort fan-out of audit events to an external SIEM. Env-gated and
fire-and-forget — a SIEM outage never blocks or fails the audited operation
(audit logging follows the same swallow-and-log discipline as logger.py).

Configure either or both:
  ROBOTHOR_SIEM_WEBHOOK_URL   — JSON POST (Splunk HEC / Datadog / generic)
  ROBOTHOR_SIEM_SYSLOG_HOST   — RFC5424 syslog over UDP (+ ROBOTHOR_SIEM_SYSLOG_PORT, default 514)
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)


def siem_enabled() -> bool:
    """True if any SIEM forwarding target is configured."""
    return bool(
        os.environ.get("ROBOTHOR_SIEM_WEBHOOK_URL") or os.environ.get("ROBOTHOR_SIEM_SYSLOG_HOST")
    )


def forward_event(event: dict[str, Any]) -> None:
    """Fan an audit event out to the configured SIEM target(s). Best-effort."""
    if not event:
        return
    webhook = os.environ.get("ROBOTHOR_SIEM_WEBHOOK_URL")
    if webhook:
        _forward_webhook(webhook, event)
    syslog_host = os.environ.get("ROBOTHOR_SIEM_SYSLOG_HOST")
    if syslog_host:
        port = int(os.environ.get("ROBOTHOR_SIEM_SYSLOG_PORT", "514"))
        _forward_syslog(syslog_host, port, event)


def _forward_webhook(url: str, event: dict[str, Any]) -> None:
    try:
        import httpx

        httpx.post(url, json=event, timeout=5.0)
    except Exception as e:
        logger.debug("SIEM webhook forward failed: %s", e)


def format_syslog(event: dict[str, Any]) -> str:
    """RFC5424 line: ``<PRI>1 - HOST APP - MSGID - JSON``. facility=user, sev=notice."""
    pri = 13  # facility 1 (user) * 8 + severity 5 (notice)
    host = socket.gethostname()
    msgid = str(event.get("event_type", "-")) or "-"
    return f"<{pri}>1 - {host} robothor - {msgid} - {json.dumps(event, default=str)}"


def _forward_syslog(host: str, port: int, event: dict[str, Any]) -> None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(format_syslog(event).encode(), (host, port))
        finally:
            sock.close()
    except Exception as e:
        logger.debug("SIEM syslog forward failed: %s", e)
