"""Datadog findings and alerts reach people, in the tenant they belong to.

Two callers, one rule: a tenant's Datadog news is delivered by that tenant's
OWN engine, because :func:`robothor.engine.alerts.alert` always delivers in
the process's own tenant (its digest row goes to this engine's main agent, its
page to this engine's operator channels). Nothing here can address another
tenant, so nothing here can deliver in one.

* :func:`send_alert` is the one call into the alerts path, and turns its
  answer into a record: ``delivered``, ``suppressed`` (the alerts cooldown
  already sent this id) or ``delivery_failed`` with the error kind. It never
  raises. The Datadog watch (:mod:`robothor.engine.datadog_watch`) uses it for
  its findings, which are always its own tenant's.
* :func:`handle_stream_event` is the ``datadog`` stream's platform handler.
  A monitor webhook carries no tenant, so the root engine consumes it, routes
  it by the monitor's ``org:`` tag, and then delivers it itself (when it is
  the root's own alert) or HANDS IT OFF: one event on the same stream,
  addressed to the routed tenant (``tenant_id``), which only that tenant's
  engine owns (``EventHooks._owns_event``). That engine routes the alert
  again from its own tags and delivers it only when the answer is itself.
  When the routed tenant has no engine reading the stream, or the hand-off
  cannot be written, the alert is recorded in that tenant as
  ``delivery_failed`` and the root gets one quiet notice naming the tenant.

Levels. A ``Triggered`` or ``Re-Triggered`` alert pages (``critical``) only
for a P1 monitor; any other trigger, and Warn or No Data, is a ``warning`` in
the digest; a recovery is ``info``.

Episodes. Datadog renotifies, retries and re-triggers. One monitor's
notifications form an episode that its recovery closes; inside one, each step
(Warn or No Data, a trigger, a P1 trigger) is sent once, a step up is sent
again, and the triage run starts once. The watch reads the same keys
(:func:`webhook_covers`) so a monitor wired to the webhook is not sent twice. The episode lives in Redis under
:data:`EPISODE_PREFIX`, kept :data:`EPISODE_TTL_SECONDS` so a recovery Datadog
never sent cannot silence a monitor for good.

Triage. A delivered ``Triggered`` or ``Re-Triggered`` alert (never Warn, No
Data or a recovery) starts ONE run of the tenant's main agent per episode in
plan mode (``AgentRunner.execute(readonly_mode=True)``): its schemas are
filtered to the read-only tools and the runtime admission gate refuses any
other. The run's result goes out through the agent's normal delivery; the run
itself is recorded as one ``datadog.triage_run`` audit event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from robothor.integrations.datadog.alerts import DatadogAlert

logger = logging.getLogger(__name__)

__all__ = [
    "DELIVERED",
    "EPISODE_PREFIX",
    "EPISODE_TTL_SECONDS",
    "FAILED",
    "HANDOFF_SOURCE",
    "HANDOFF_TYPE",
    "SUPPRESSED",
    "TRIAGE_TRANSITIONS",
    "handle_stream_event",
    "reset_state",
    "send_alert",
    "wait_for_triage",
    "webhook_covers",
]

DELIVERED, SUPPRESSED, FAILED = "delivered", "suppressed", "delivery_failed"

#: The event the root engine writes to hand an alert to its tenant's engine.
HANDOFF_TYPE = "datadog.deliver"
#: Its ``source``: a webhook never carries it, so a webhook is never a hand-off.
HANDOFF_SOURCE = "datadog_handoff"
_STREAM = "datadog"

#: The transitions that start the read-only triage run.
TRIAGE_TRANSITIONS = frozenset({"triggered", "re_triggered"})
_RECOVERIES = frozenset({"recovered", "warn_recovered", "no_data_recovered"})
#: The steps of an episode, lowest first: Warn or No Data, a trigger, a P1
#: trigger (the one that pages). Each is sent once per episode, and sending
#: one covers the steps below it.
_STAGES = ("warn", "alert", "page")

EPISODE_PREFIX = "robothor:datadog:episode:"
#: How long an episode without a recovery is remembered: a staleness bound,
#: not a limit on anything.
EPISODE_TTL_SECONDS = 7 * 86400

#: How each transition reads in a brief.
_STATE = {
    "triggered": "in Alert",
    "re_triggered": "still in Alert",
    "warn": "in Warn",
    "re_warn": "still in Warn",
    "no_data": "in No Data",
    "re_no_data": "still in No Data",
    "recovered": "recovered",
    "warn_recovered": "recovered from Warn",
    "no_data_recovered": "reporting data again",
}
_PRIORITY = re.compile(r"^P[1-5]$")

_triage_tasks: set[asyncio.Task[Any]] = set()


def reset_state() -> None:
    """Forget the triage runs in flight (tests)."""
    _triage_tasks.clear()


async def wait_for_triage() -> None:
    """Return once every triage run started here has finished (tests)."""
    while _triage_tasks:
        await asyncio.wait(set(_triage_tasks))


# ── the alerts path ─────────────────────────────────────────────────


async def send_alert(
    level: str,
    title: str,
    body: str,
    *,
    alert_id: str,
    kind: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send once through :func:`robothor.engine.alerts.alert`; the record of what happened.

    ``{"mode": ..., "delivery": {"level", "route", "alert_id", ...}}``. A repeat
    of an id the alerts path delivered inside its cooldown is ``suppressed``
    (reason ``cooldown``) and not sent. Never raises.
    """
    from robothor.engine import alerts

    route = "page" if level in alerts._PAGE_LEVELS else "digest"
    delivery: dict[str, Any] = {"level": level, "route": route, "alert_id": alert_id}
    if alerts._suppressed(alert_id):
        return {"mode": SUPPRESSED, "delivery": {**delivery, "reason": "cooldown"}}
    try:
        sent = await alerts.alert(
            level, title, body, alert_id=alert_id, kind=kind, metadata=metadata
        )
    except Exception as exc:  # noqa: BLE001 - a failed send is recorded, never raised
        logger.warning("Datadog delivery of %s raised (%s)", alert_id, type(exc).__name__)
        return {"mode": FAILED, "delivery": {**delivery, "error": type(exc).__name__}}
    if not sent:
        return {"mode": FAILED, "delivery": {**delivery, "error": "not_delivered"}}
    return {"mode": DELIVERED, "delivery": delivery}


# ── the stream handler ──────────────────────────────────────────────


def _own_tenant(hooks: Any) -> str:
    return str(getattr(hooks.config, "tenant_id", "") or DEFAULT_TENANT)


async def handle_stream_event(decoded: dict[str, Any], redis_client: Any, hooks: Any) -> str:
    """Route, deliver or hand off one event of the ``datadog`` stream.

    ``hooks`` is the consuming :class:`~robothor.engine.hooks.EventHooks`,
    which owns the event (its tenant is the event's). Raises only when the
    alert could not be recorded, so the event goes to the stream's DLQ.
    """
    from robothor.integrations.datadog.alerts import handle_alert_event

    raw = decoded.get("payload", "")
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = {}
    own = _own_tenant(hooks)

    if decoded.get("type") == HANDOFF_TYPE or decoded.get("source") == HANDOFF_SOURCE:
        if (
            decoded.get("type") != HANDOFF_TYPE
            or decoded.get("source") != HANDOFF_SOURCE
            or decoded.get("tenant_id") != own
        ):
            logger.warning("Datadog hand-off refused: not a hand-off addressed to %s", own)
            return "refused"

        async def deliver_handed_off(
            alert: DatadogAlert, tenant: str, routing: str, title: str
        ) -> dict[str, Any]:
            # Routed again from the alert's own tags: only an org-tagged alert
            # for this very tenant is delivered here, whatever the envelope says.
            if routing != "org" or tenant != own:
                return {"mode": "refused"}
            return await _deliver_here(alert, own, title, redis_client, hooks)

        return await handle_alert_event(
            payload, redis_client, deliver=deliver_handed_off, dedup_scope=f"delivered:{own}"
        )

    async def route(alert: DatadogAlert, tenant: str, routing: str, title: str) -> dict[str, Any]:
        if tenant == own:
            return await _deliver_here(alert, own, title, redis_client, hooks)
        return await _hand_off(payload, tenant, redis_client)

    return await handle_alert_event(payload, redis_client, deliver=route)


# ── the hand-off ────────────────────────────────────────────────────


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _field(info: Any, name: str) -> Any:
    if not isinstance(info, dict):
        return None
    return info.get(name, info.get(name.encode()))


async def _engine_running(redis_client: Any, tenant: str) -> bool:
    """Whether ``tenant``'s engine read the ``datadog`` stream in the last minute.

    Engines name their consumer ``engine-<tenant>`` in whatever group they
    read with, so every group on the stream is looked at.
    """
    from robothor.engine.hooks import GROUP_RIVAL_ACTIVE_MS, STREAM_PREFIX

    key = f"{STREAM_PREFIX}{_STREAM}"
    consumer = f"engine-{tenant}"
    try:
        for group in await redis_client.xinfo_groups(key) or []:
            name = _text(_field(group, "name"))
            for info in await redis_client.xinfo_consumers(key, name) or []:
                idle = int(_field(info, "idle") or 0)
                if _text(_field(info, "name")) == consumer and idle < GROUP_RIVAL_ACTIVE_MS:
                    return True
    except Exception as exc:  # noqa: BLE001 - unknown is not running
        logger.info("Datadog hand-off: readers of the stream unknown (%s)", type(exc).__name__)
    return False


async def _hand_off(payload: Any, tenant: str, redis_client: Any) -> dict[str, Any]:
    """Address the alert to ``tenant``'s own engine, or say why it could not be."""
    from robothor.engine.hooks import STREAM_PREFIX

    if redis_client is None:
        reason = "handoff_failed"
    elif not await _engine_running(redis_client, tenant):
        reason = "no_engine"
    else:
        envelope = {
            "id": str(uuid.uuid4()),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "type": HANDOFF_TYPE,
            "event_type": HANDOFF_TYPE,
            "source": HANDOFF_SOURCE,
            "actor": "engine",
            "payload": json.dumps(payload),
            "correlation_id": "",
            "tenant_id": tenant,
        }
        try:
            await redis_client.xadd(
                f"{STREAM_PREFIX}{_STREAM}", envelope, maxlen=10000, approximate=True
            )
            return {"mode": "handed_off", "handoff": {"tenant": tenant}}
        except Exception as exc:  # noqa: BLE001 - recorded as a failed hand-off
            logger.warning("Datadog hand-off to %s failed (%s)", tenant, type(exc).__name__)
            reason = "handoff_failed"
    await _tell_root(tenant, reason)
    return {
        "mode": FAILED,
        "delivery": {"reason": reason, "tenant": tenant},
        "triage": {"started": False, "reason": reason},
    }


async def _tell_root(tenant: str, reason: str) -> None:
    """One quiet notice in this (the root's) engine; it names the tenant, never the alert."""
    why = (
        "no engine of that tenant is reading Datadog alerts"
        if reason == "no_engine"
        else "it could not be handed to that tenant's engine"
    )
    await send_alert(
        "info",
        f"Datadog alert not delivered: tenant {tenant}",
        f"A Datadog alert for tenant {tenant} was recorded there and not sent: {why}. "
        "Check that the tenant's engine runs with ROBOTHOR_DATADOG_SCOPES set.",
        alert_id=f"datadog_undelivered:{tenant}",
        kind="datadog_alert_undelivered",
    )


# ── delivery in this engine's own tenant ────────────────────────────


async def _claim(redis_client: Any, key: str) -> bool | None:
    """``SET NX``: True when this call took ``key``, False when taken, None when unknown."""
    if redis_client is None:
        return None
    try:
        return bool(await redis_client.set(key, 1, nx=True, ex=EPISODE_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001 - an unknown episode still delivers
        logger.warning("Datadog episode check failed (%s)", type(exc).__name__)
        return None


async def _forget(redis_client: Any, *keys: str) -> None:
    if redis_client is None or not keys:
        return
    try:
        await redis_client.delete(*keys)
    except Exception as exc:  # noqa: BLE001 - a stale key costs one suppressed repeat
        logger.warning("Datadog episode keys not cleared (%s)", type(exc).__name__)


async def _mark(redis_client: Any, *keys: str) -> None:
    if redis_client is None:
        return
    for key in keys:
        try:
            await redis_client.set(key, 1, ex=EPISODE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - costs one repeat at a lower level
            logger.warning("Datadog episode key not set (%s)", type(exc).__name__)


def _brief(alert: DatadogAlert, title: str) -> str:
    """Names only: the masked title, the monitor id, its state, a P1-P5 priority."""
    state = _STATE.get(alert.transition, "changed state")
    priority = f" ({alert.priority})" if _PRIORITY.match(alert.priority or "") else ""
    return (
        f'Datadog monitor "{title or "untitled"}" ({alert.monitor_id or "-"}) is {state}'
        f"{priority}. Check it in Datadog."
    )


def _stage(transition: str, level: str) -> str:
    """The episode step a transition is: ``recovered``, ``page``, ``alert`` or ``warn``."""
    if transition in _RECOVERIES:
        return "recovered"
    if transition in TRIAGE_TRANSITIONS:
        return "page" if level == "critical" else "alert"
    return "warn"


async def webhook_covers(
    redis_client: Any, tenant: str, monitor_id: str, *, recovery: bool
) -> bool:
    """Whether the webhook has an open episode for this monitor (or just closed one).

    The watch asks before it sends a ``monitor_firing`` (open: any step sent)
    or a ``monitor_recovered`` (open, or closed by a recovery the webhook
    sent). Raises when Redis cannot be read: the caller decides.
    """
    base = f"{EPISODE_PREFIX}{tenant}:{monitor_id}"
    keys = [f"{base}:sent:{name}" for name in _STAGES]
    if recovery:
        keys.append(f"{base}:sent:recovered")
    return bool(await redis_client.exists(*keys))


async def _deliver_here(
    alert: DatadogAlert, tenant: str, title: str, redis_client: Any, hooks: Any
) -> dict[str, Any]:
    """Deliver an alert of this engine's own tenant, once per level per episode."""
    from robothor.integrations.datadog.alerts import level_for

    level = level_for(alert.transition, alert.priority)
    monitor = alert.monitor_id or "-"
    base = f"{EPISODE_PREFIX}{tenant}:{monitor}"
    recovery = alert.transition in _RECOVERIES
    stage = _stage(alert.transition, level)
    gate = f"{base}:sent:{stage}"
    claimed = await _claim(redis_client, gate)
    if claimed is False:
        return {
            "mode": SUPPRESSED,
            "delivery": {"level": level, "reason": "episode_open"},
            "triage": {"started": False, "reason": "episode_open"},
        }
    outcome = await send_alert(
        level,
        f"Datadog monitor: {title or monitor}",
        _brief(alert, title),
        alert_id=f"datadog_alert:{tenant}:{monitor}:{stage}",
        kind="datadog_alert",
        metadata={"tenant": tenant, "monitor_id": monitor, "transition": alert.transition},
    )
    if outcome["mode"] != DELIVERED:
        if outcome["mode"] == FAILED and claimed:
            await _forget(redis_client, gate)  # the next notification tries again
        outcome["triage"] = {"started": False, "reason": outcome["mode"]}
        return outcome
    if recovery:
        stages = [f"{base}:sent:{name}" for name in _STAGES]
        await _forget(redis_client, *stages, f"{base}:triage")
        outcome["triage"] = {"started": False, "reason": "recovery"}
        return outcome
    await _forget(redis_client, f"{base}:sent:recovered")
    # A step sent covers the steps below it for the rest of the episode.
    await _mark(redis_client, *(f"{base}:sent:{n}" for n in _STAGES[: _STAGES.index(stage)]))
    outcome["triage"] = await _maybe_triage(
        alert, tenant, title, redis_client, hooks, base, claimed
    )
    return outcome


async def _maybe_triage(
    alert: DatadogAlert,
    tenant: str,
    title: str,
    redis_client: Any,
    hooks: Any,
    base: str,
    claimed: bool | None,
) -> dict[str, Any]:
    if alert.transition not in TRIAGE_TRANSITIONS:
        return {"started": False, "reason": "not_a_trigger"}
    if claimed is None:
        # Without the episode store a storm could start a run per notification.
        return {"started": False, "reason": "episode_unknown"}
    if not await _claim(redis_client, f"{base}:triage"):
        return {"started": False, "reason": "episode_open"}
    started = await _start_triage(alert, tenant, title, hooks)
    if not started.get("started"):
        await _forget(redis_client, f"{base}:triage")
    return started


# ── the triage run ──────────────────────────────────────────────────


async def _start_triage(alert: DatadogAlert, tenant: str, title: str, hooks: Any) -> dict[str, Any]:
    """Start one read-only run of this tenant's main agent in the background."""
    from robothor.engine.config import load_agent_config
    from robothor.engine.manifest_store import main_agent_id
    from robothor.engine.models import TriggerType
    from robothor.engine.task_registry import get_task_registry
    from robothor.integrations.datadog.alerts import triage_prompt

    # EngineConfig.from_env reads the environment and the disk: off the loop.
    agent = await asyncio.to_thread(main_agent_id)
    if not agent:
        return {"started": False, "reason": "no_main_agent"}
    try:
        config = await asyncio.to_thread(
            load_agent_config, agent, hooks.config.manifest_dir, trigger_type=TriggerType.HOOK
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the alert
        return {"started": False, "reason": "no_agent_config", "error": type(exc).__name__}
    if config is None:
        return {"started": False, "reason": "no_agent_config", "agent": agent}
    prompt = triage_prompt(alert, title=title)
    monitor = alert.monitor_id or "-"
    task = get_task_registry().spawn(
        _run_triage(hooks.runner, agent, config, prompt, tenant, monitor),
        name=f"datadog-triage:{tenant}:{monitor}",
    )
    _triage_tasks.add(task)
    task.add_done_callback(_triage_tasks.discard)
    return {"started": True, "agent": agent, "read_only": True}


async def _run_triage(
    runner: Any, agent: str, config: Any, prompt: str, tenant: str, monitor: str
) -> None:
    """The run itself; whatever happens is one ``datadog.triage_run`` event."""
    from robothor.engine import dedup, delivery
    from robothor.engine.models import TriggerType

    status, error = "agent_busy", ""
    if await dedup.try_acquire(agent):
        try:
            run = await runner.execute(
                agent_id=agent,
                message=prompt,
                trigger_type=TriggerType.HOOK,
                trigger_detail=f"datadog:triage:{monitor}",
                agent_config=config,
                readonly_mode=True,
                tenant_id=tenant,
            )
            await delivery.deliver(config, run)
            status = str(run.status.value)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            status, error = "failed", type(exc).__name__
            logger.warning("Datadog triage run for monitor %s failed (%s)", monitor, error)
        finally:
            await dedup.release(agent)
    _record_triage(tenant, monitor, agent, status, error)


def _record_triage(tenant: str, monitor: str, agent: str, status: str, error: str) -> None:
    from robothor.audit import logger as audit
    from robothor.engine.event_writer import get_event_writer

    details: dict[str, Any] = {
        "monitor_id": monitor,
        "agent": agent,
        "status": status,
        "read_only": True,
    }
    if error:
        details["error"] = error
    get_event_writer().submit(
        audit.log_event,
        "datadog.triage_run",
        status,
        category="datadog",
        actor="engine",
        details=details,
        target=f"datadog_monitor:{monitor}",
        tenant_id=tenant,
    )
