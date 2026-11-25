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
3. hands the alert to the engine's deliverer
   (:mod:`robothor.engine.datadog_delivery`), which delivers it in that
   tenant, hands it to that tenant's own engine, or says why it could not;
4. records ONE ``datadog.alert_received`` audit event in that tenant per
   (monitor, transition, date) with the outcome: ``mode`` is ``delivered``,
   ``suppressed`` (its episode or the alerts cooldown already covered it) or
   ``delivery_failed``, beside ``delivery`` and ``triage``. A hand-off is
   recorded by the engine that delivers it, not here. A recovery is its own
   event with the same monitor id, so a reader can pair it with the trigger.

Records written before delivery existed say ``mode: "dry_run"``; a reader
treats anything that is not ``delivered`` as recorded and not sent.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_TYPE",
    "NOT_AVAILABLE",
    "STREAM",
    "AlertRecordError",
    "DatadogAlert",
    "handle_alert_event",
    "is_p1",
    "level_for",
    "parse_alert",
    "route_tenant",
    "triage_prompt",
    "would_notify",
]

#: The stream the ``datadog`` webhook channel publishes to.
STREAM = "datadog"
#: The audit event type of one recorded transition.
EVENT_TYPE = "datadog.alert_received"
#: The tag that names the tenant a monitor belongs to.
ORG_TAG = "org:"
#: The outcome when no deliverer was given: recorded, not sent.
NOT_AVAILABLE: dict[str, Any] = {"mode": "delivery_failed", "delivery": {"reason": "not_available"}}
#: Outcomes this engine does not record: a hand-off (the routed tenant's
#: engine records it) and a refusal (it does not belong here).
_NOT_RECORDED_HERE = frozenset({"handed_off", "refused"})
#: How long a notification is remembered against Datadog's retries.
DEDUP_TTL_SECONDS = 86400
#: How long a claim lives before its audit event is written. Another copy that
#: finds it is a duplicate; the stream message that made it may finish it.
PENDING_TTL_SECONDS = 120
_DEDUP_PREFIX = "robothor:datadog:alert:"

#: A field-fit bound so one odd payload cannot make a huge audit row; real
#: monitor titles and links are far shorter.
_FIELD_CHARS = 500

#: Transition -> the alert level the normal alerts path uses. A trigger is
#: ``critical`` (it pages) only for a P1 monitor: see :func:`level_for`.
_LEVELS = {
    "triggered": "warning",
    "re_triggered": "warning",
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
    'Datadog monitor {monitor} ("{title}") triggered at {date}. Take a first look with '
    "the Datadog tools (logs, monitors, spans, metrics) inside this tenant's scope, and "
    "say briefly what is happening and what likely caused it. This is a read-only run: "
    "change nothing. If you do not have the Datadog tools, say so in one line and stop."
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


#: The transitions that page when the monitor is P1.
_TRIGGERS = frozenset({"triggered", "re_triggered"})


def is_p1(priority: str) -> bool:
    """Whether Datadog's ``$ALERT_PRIORITY`` is P1 (``P1``, ``p1`` or ``1``)."""
    return (priority or "").strip().lower() in ("p1", "1")


def level_for(transition: str, priority: str = "") -> str:
    """The alert level of a transition; an unrecognised one is a warning.

    A ``Triggered`` or ``Re-Triggered`` alert is ``critical`` (it pages) only
    when the monitor's priority is P1; any other trigger is a ``warning``
    (the digest). Warn and No Data are ``warning``, recoveries ``info``.
    """
    if transition in _TRIGGERS and is_p1(priority):
        return "critical"
    return _LEVELS.get(transition, "warning")


def would_notify(transition: str, priority: str = "") -> dict[str, Any]:
    """Who the normal alerts path (:func:`robothor.engine.alerts.alert`) would reach.

    That path is instance-wide: a ``critical`` alert pages the operator on
    Telegram and then each configured fallback channel, anything else becomes
    an ``alert_digest`` row in the main agent's inbox. Channel names only,
    never their targets.
    """
    from robothor.engine.alerts import _PAGE_LEVELS, alert_fallback_channels

    level = level_for(transition, priority)
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


def triage_prompt(alert: DatadogAlert, *, title: str) -> str:
    """The fixed prompt of the read-only triage run: the monitor, its masked title, the time."""
    return _TRIAGE_PROMPT.format(
        monitor=alert.monitor_id or "-",
        title=title or "untitled",
        date=alert.date or "an unknown time",
    )


def _dedup_key(alert: DatadogAlert, payload: Any, scope: str = "") -> str:
    # ``$ID`` names the notification: a retry resends it, another group or
    # occurrence of the same monitor carries a new one.
    if alert.event_id or alert.monitor_id or alert.transition or alert.date:
        basis = "\x1f".join((alert.event_id, alert.monitor_id, alert.transition, alert.date))
    else:
        basis = json.dumps(payload, sort_keys=True, default=str)
    prefix = f"{_DEDUP_PREFIX}{scope}:" if scope else _DEDUP_PREFIX
    return prefix + hashlib.sha256(basis.encode()).hexdigest()


def _record(
    alert: DatadogAlert, tenant: str, routing: str, title: str, outcome: dict[str, Any]
) -> None:
    """Write the audit event with the delivery outcome. Blocking (audit write)."""
    from robothor.audit.logger import log_event
    from robothor.secrets.redaction import redact_personal_data

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
        "mode": outcome.get("mode", "delivery_failed"),
        "delivery": outcome.get("delivery", {}),
        "triage": outcome.get("triage", {"started": False}),
        # The two fields records carried before delivery, kept so their
        # readers go on working: who was (or would have been) told, and the run.
        "would_notify": would_notify(alert.transition, alert.priority),
        "would_run": {
            "tenant": tenant,
            "agent": (outcome.get("triage") or {}).get("agent"),
            "read_only": True,
            "prompt": triage_prompt(alert, title=title),
        },
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


def _routed(alert: DatadogAlert) -> tuple[str, str, str]:
    """``(tenant, routing, masked title)``. Blocking (one tenant read)."""
    from robothor.secrets.redaction import redact_personal_data

    tenant, routing = route_tenant(alert.tags)
    return tenant, routing, redact_personal_data(alert.title)


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


#: ``deliver(alert, tenant, routing, masked title) -> outcome``.
Deliverer = Callable[[DatadogAlert, str, str, str], Awaitable[dict[str, Any]]]


async def _outcome(
    deliver: Deliverer | None, alert: DatadogAlert, tenant: str, routing: str, title: str
) -> dict[str, Any]:
    """What the deliverer did; one that raises is a failed delivery, never an error."""
    if deliver is None:
        return dict(NOT_AVAILABLE)
    try:
        return await deliver(alert, tenant, routing, title)
    except Exception as exc:  # noqa: BLE001 - a failed delivery is recorded, not raised
        logger.warning("Datadog alert delivery raised (%s)", type(exc).__name__)
        return {"mode": "delivery_failed", "delivery": {"error": type(exc).__name__}}


#: Outcomes after which something left this engine (an alert, a hand-off), so
#: the claim is never released: a retry must not send it again.
_SENT = frozenset({"delivered", "handed_off"})


async def handle_alert_event(
    payload: Any,
    redis_client: Any,
    *,
    message_id: str | None = None,
    deliver: Deliverer | None = None,
    dedup_scope: str = "",
) -> str:
    """Claim, deliver, record and keep one Datadog alert.

    Returns ``"recorded"``, ``"duplicate"``, ``"handed_off"`` (the routed
    tenant's engine records and delivers it) or ``"refused"`` (the deliverer
    says it does not belong to this engine: nothing is recorded or kept).
    Without a deliverer the alert is recorded as not delivered
    (``not_available``). ``dedup_scope`` keeps a separate set of keys, so the
    engine a hand-off is addressed to does not take it for a retry.

    1. Claim: ``SET NX`` a pending key that lives :data:`PENDING_TTL_SECONDS`.
       Of two copies handled at once only one claims it; the other is a
       duplicate, so one claim is at most one delivery.
    2. Deliver (never raises).
    3. Write the audit event with the real ``mode``.
    4. Keep the key, now ``done``, for :data:`DEDUP_TTL_SECONDS`.

    ``message_id`` is the stream entry being handled, and the claim holds it.
    When the consumer group redelivers that same entry after a crash between
    the claim and the write, the claim is this entry's to finish rather than a
    duplicate, so a crash loses nothing; the deliverer is what keeps such a
    replay from sending twice (its episode keys hold the same entry id).
    Another entry, or a ``done`` key, is a duplicate. Without ``message_id``,
    any claim is a duplicate.

    A failure before anything was sent (routing, or the write after a failed,
    suppressed or refused delivery) releases the claim, so a retry is
    accepted. When the write fails after the alert was delivered or handed
    off, the claim is kept for its pending window, so no retry sends it again,
    and :class:`AlertRecordError` sends the event to the dead letter stream.
    """
    alert = parse_alert(payload)
    key = _dedup_key(alert, payload, dedup_scope)
    if not await _claim(redis_client, key, message_id or None):
        logger.info("Datadog alert for monitor %s already recorded", alert.monitor_id or "-")
        return "duplicate"
    try:
        tenant, routing, title = await asyncio.to_thread(_routed, alert)
    except BaseException:
        await _release(redis_client, key)
        raise
    outcome = await _outcome(deliver, alert, tenant, routing, title)
    mode = str(outcome.get("mode", "delivery_failed"))
    if mode == "refused":
        await _release(redis_client, key)
        logger.warning(
            "Datadog alert for monitor %s refused here (it routes to %s, %s)",
            alert.monitor_id or "-",
            tenant,
            routing,
        )
        return "refused"
    if mode not in _NOT_RECORDED_HERE:
        try:
            await asyncio.to_thread(_record, alert, tenant, routing, title, outcome)
        except BaseException:
            if mode not in _SENT:
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
            mode=mode,
            routing=routing,
        ),
    )
    return "handed_off" if mode == "handed_off" else "recorded"
