"""Datadog monitor alerts into Genus, routed to the tenant the monitor belongs to.

Datadog's webhook integration calls the ``datadog`` webhook channel (scheme
``header_secret``) when a monitor changes state. The receiver publishes the
body to the ``datadog`` stream like any webhook; the engine that owns the
event hands it to :func:`handle_alert_event`, which:

1. parses the body defensively (:func:`parse_alert`): a missing or odd field
   is an empty value, never an error;
2. routes it by the monitor's ``org:<tenant id>`` tag and nothing else
   (:func:`route_tenant`): an active tenant gets it, anything else goes to the
   root tenant marked ``untagged``, ``unknown_org:<value>`` or
   ``ambiguous_org:<a>,<b>``;
3. records ONE ``datadog.alert_received`` audit event in that tenant per
   (monitor, transition, date), with who the normal alerts path would notify
   (:func:`would_notify`) and the read-only triage run it would start
   (:func:`triage_plan`). A recovery is its own event with the same monitor
   id, so a reader can pair it with the trigger.

Recording is the only thing this does: nothing is delivered to anyone and no
run is started. Every record says ``mode: "dry_run"`` (:data:`MODE`) so it
can be told apart from delivered alerts once delivery ships as its own change.

Read the records back with :func:`robothor.audit.logger.query_log`
(``event_type="datadog.alert_received"``, ``tenants=[...]``), which is what
the bridge's ``GET /api/audit/events`` serves.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_TYPE",
    "STREAM",
    "AlertRecordError",
    "DatadogAlert",
    "handle_alert_event",
    "parse_alert",
    "route_tenant",
    "triage_plan",
    "would_notify",
]

#: The stream the ``datadog`` webhook channel publishes to.
STREAM = "datadog"
#: The audit event type of one recorded transition.
EVENT_TYPE = "datadog.alert_received"
#: The tag that names the tenant a monitor belongs to.
ORG_TAG = "org:"
#: What every record says it is: recorded, not delivered.
MODE = "dry_run"
#: How long a notification is remembered against Datadog's retries.
DEDUP_TTL_SECONDS = 86400
#: How long a claim lives before its audit event is written. Another copy that
#: finds it is a duplicate; the stream message that made it may finish it.
PENDING_TTL_SECONDS = 120
_DEDUP_PREFIX = "robothor:datadog:alert:"

#: A field-fit bound so one odd payload cannot make a huge audit row; real
#: monitor titles and links are far shorter.
_FIELD_CHARS = 500

#: Transition -> the alert level the normal alerts path would use.
_LEVELS = {
    "triggered": "critical",
    "re_triggered": "critical",
    "warn": "warning",
    "re_warn": "warning",
    "no_data": "warning",
    "re_no_data": "warning",
    "recovered": "info",
    "warn_recovered": "info",
    "no_data_recovered": "info",
}

_NOT_A_WORD = re.compile(r"[^a-z0-9]+")

_TRIAGE_PROMPT = (
    "Datadog monitor {monitor} ({title}) changed to {transition} at {date}. "
    "Take a read-only look at this tenant's logs around that time with the Datadog log "
    "tools, and summarise what you find and what likely caused it. Change nothing."
)


class AlertRecordError(RuntimeError):
    """The alert could not be recorded; the caller keeps the event."""


@dataclass(frozen=True)
class DatadogAlert:
    """One monitor transition, as Datadog's webhook body carried it."""

    monitor_id: str
    event_id: str
    title: str
    transition: str
    raw_transition: str
    status: str
    priority: str
    tags: tuple[str, ...]
    link: str
    date: str


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:_FIELD_CHARS]


def _tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        items = [_text(v) for v in value]
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = []
    return tuple(t[:_FIELD_CHARS] for t in items if t)


def _transition(raw: str) -> str:
    return _NOT_A_WORD.sub("_", raw.lower()).strip("_")


def parse_alert(payload: Any) -> DatadogAlert:
    """The alert in a webhook body; a missing field is ``""``, a non-object body is empty."""
    body = payload if isinstance(payload, dict) else {}
    raw_transition = _text(body.get("transition"))
    return DatadogAlert(
        monitor_id=_text(body.get("monitor_id")) or _text(body.get("alert_id")),
        event_id=_text(body.get("event_id")),
        title=_text(body.get("title")),
        transition=_transition(raw_transition),
        raw_transition=raw_transition,
        status=_text(body.get("status")),
        priority=_text(body.get("priority")),
        tags=_tags(body.get("tags")),
        link=_text(body.get("link")),
        date=_text(body.get("date")),
    )


def _active_tenant(tenant_id: str) -> bool:
    from robothor.crm.dal import get_tenant

    try:
        row = get_tenant(tenant_id)
    except Exception as exc:  # noqa: BLE001 — an unreadable tenant routes to the root
        logger.warning("Datadog alert: tenant lookup failed (%s)", type(exc).__name__)
        return False
    return row is not None and row.get("active", True) is not False


def route_tenant(tags: tuple[str, ...]) -> tuple[str, str]:
    """``(tenant id, routing)`` from the monitor's tags alone. Blocking (one tenant read).

    ``routing`` is ``org`` when the ``org:`` tag names an active tenant, else
    the alert goes to the root tenant and ``routing`` says why.
    """
    from robothor.constants import DEFAULT_TENANT

    orgs = sorted({t[len(ORG_TAG) :].strip() for t in tags if t.lower().startswith(ORG_TAG)} - {""})
    if not orgs:
        return DEFAULT_TENANT, "untagged"
    if len(orgs) > 1:
        return DEFAULT_TENANT, "ambiguous_org:" + ",".join(orgs)
    org = orgs[0]
    if _active_tenant(org):
        return org, "org"
    return DEFAULT_TENANT, f"unknown_org:{org}"


def level_for(transition: str) -> str:
    """The alert level a transition maps to; an unrecognised one is a warning."""
    return _LEVELS.get(transition, "warning")


def would_notify(transition: str) -> dict[str, Any]:
    """Who the normal alerts path (:func:`robothor.engine.alerts.alert`) would reach.

    That path is instance-wide: a ``critical`` alert pages the operator on
    Telegram and then each configured fallback channel, anything else becomes
    an ``alert_digest`` row in the main agent's inbox. Channel names only,
    never their targets.
    """
    from robothor.engine.alerts import _PAGE_LEVELS, alert_fallback_channels

    level = level_for(transition)
    if level in _PAGE_LEVELS:
        channels = ["telegram", *(name for name, _ in alert_fallback_channels())]
        return {
            "level": level,
            "route": "page",
            "channels": channels,
            "audience": "the instance operator",
        }
    return {
        "level": level,
        "route": "digest",
        "channels": ["notification:main"],
        "audience": "the main agent's inbox (alert_digest), read by its heartbeat",
    }


def _own_tenant() -> str:
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import effective_tenant

    return effective_tenant() or DEFAULT_TENANT


def triage_plan(tenant_id: str, alert: DatadogAlert, *, title: str | None = None) -> dict[str, Any]:
    """The read-only run delivery would start for this alert.

    ``agent`` is this engine's main agent when the alert is this engine's own
    tenant's; another tenant's main agent is its own engine's to name, so it
    is ``None`` here.
    """
    agent: str | None = None
    if tenant_id == _own_tenant():
        from robothor.engine.manifest_store import main_agent_id

        agent = main_agent_id() or None
    shown = title if title is not None else alert.title
    return {
        "tenant": tenant_id,
        "agent": agent,
        "read_only": True,
        "prompt": _TRIAGE_PROMPT.format(
            monitor=alert.monitor_id or "-",
            title=shown or "untitled",
            transition=alert.transition or "unknown",
            date=alert.date or "an unknown time",
        ),
    }


def _dedup_key(alert: DatadogAlert, payload: Any) -> str:
    # ``$ID`` names the notification: a retry resends it, another group or
    # occurrence of the same monitor carries a new one.
    if alert.event_id or alert.monitor_id or alert.transition or alert.date:
        basis = "\x1f".join((alert.event_id, alert.monitor_id, alert.transition, alert.date))
    else:
        basis = json.dumps(payload, sort_keys=True, default=str)
    return _DEDUP_PREFIX + hashlib.sha256(basis.encode()).hexdigest()


def _record(alert: DatadogAlert) -> tuple[str, str]:
    """Write the audit event; ``(tenant, routing)``. Blocking (tenant read, audit write)."""
    from robothor.audit.logger import log_event
    from robothor.secrets.redaction import redact_personal_data

    tenant, routing = route_tenant(alert.tags)
    title = redact_personal_data(alert.title)
    details = {
        "monitor_id": alert.monitor_id,
        "event_id": alert.event_id,
        "title": title,
        "transition": alert.transition,
        "datadog_transition": alert.raw_transition,
        "status": alert.status,
        "priority": alert.priority,
        "tags": [redact_personal_data(t) for t in alert.tags],
        "link": alert.link,
        "date": alert.date,
        "routing": routing,
        "mode": MODE,
        "would_notify": would_notify(alert.transition),
        "would_run": triage_plan(tenant, alert, title=title),
    }
    written = log_event(
        EVENT_TYPE,
        f"datadog monitor {alert.monitor_id or '-'} {alert.transition or 'unknown'}",
        category="datadog",
        actor="datadog",
        details=details,
        source_channel="webhook",
        target=f"datadog_monitor:{alert.monitor_id}" if alert.monitor_id else "datadog_monitor",
        tenant_id=tenant,
    )
    if written is None:
        raise AlertRecordError("the audit event could not be written")
    return tenant, routing


#: The claim's value once the audit event is written.
_DONE = "done"
#: The claim's value when the caller names no stream message.
_PENDING = "pending"


def _text_of(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _claim(redis_client: Any, key: str, owner: str | None) -> bool:
    """Claim the notification with a short-lived pending key; ``False`` for a duplicate.

    The claim's value is the stream message that made it. ``SET NX`` is
    atomic, so of two copies handled at once only one claims it. A claim that
    names THIS message was left by an earlier attempt at it that never wrote
    (the engine died and the consumer group redelivered the message), so it
    is this message's to finish. A claim by another message, or one already
    ``done``, is a duplicate. No Redis: record anyway (a weaker filter, never
    a lost alert).
    """
    if redis_client is None:
        return True
    try:
        value = owner or _PENDING
        if await redis_client.set(key, value, nx=True, ex=PENDING_TTL_SECONDS):
            return True
        if owner is None:
            return False
        held = await redis_client.get(key)
        return held is not None and _text_of(held) == owner
    except Exception as exc:  # noqa: BLE001 — dedup is a filter, the record is the point
        logger.warning("Datadog alert dedup check failed (%s); recording", type(exc).__name__)
        return True


async def _keep(redis_client: Any, key: str) -> None:
    """The event is written: remember the notification for the full window."""
    if redis_client is None:
        return
    try:
        await redis_client.set(key, _DONE, ex=DEDUP_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the pending claim still covers its window
        logger.warning("Datadog alert dedup key not extended (%s)", type(exc).__name__)


async def _release(redis_client: Any, key: str) -> None:
    """The write failed: drop the claim so a retry can record the alert."""
    if redis_client is None:
        return
    try:
        await redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001 — the claim lapses after its pending window
        logger.warning("Datadog alert dedup release failed (%s)", type(exc).__name__)


async def handle_alert_event(
    payload: Any, redis_client: Any, *, message_id: str | None = None
) -> str:
    """Record one Datadog alert; ``"recorded"`` or ``"duplicate"``.

    Claim, write, extend: the notification is claimed with a pending key that
    lives :data:`PENDING_TTL_SECONDS`, so a second copy handled at the same
    time is a duplicate; the audit event is written; then the key is kept for
    :data:`DEDUP_TTL_SECONDS`. A failed write deletes the claim and raises
    :class:`AlertRecordError`.

    ``message_id`` is the stream entry being handled, and the claim records
    it. When the consumer group redelivers that same entry after a crash
    between the claim and the write, the alert is recorded rather than taken
    for a duplicate, so a crash loses nothing. A crash after the write but
    before the extension can record the alert once more: at once when the
    engine redelivers the same entry, or when another copy arrives after the
    pending window. Without ``message_id``, any claim is a
    duplicate.
    """
    alert = parse_alert(payload)
    key = _dedup_key(alert, payload)
    if not await _claim(redis_client, key, message_id or None):
        logger.info("Datadog alert for monitor %s already recorded", alert.monitor_id or "-")
        return "duplicate"
    try:
        tenant, routing = await asyncio.to_thread(_record, alert)
    except BaseException:
        await _release(redis_client, key)
        raise
    await _keep(redis_client, key)
    from robothor.engine.log_fields import kv_line

    logger.info(
        "%s",
        kv_line(
            "datadog_alert",
            tenant=tenant,
            monitor_id=alert.monitor_id,
            transition=alert.transition,
            mode=MODE,
            routing=routing,
        ),
    )
    return "recorded"
