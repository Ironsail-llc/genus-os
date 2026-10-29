"""The daemon worker that watches an n8n instance.

Enabled ONLY when ``ROBOTHOR_N8N_ENABLED`` is on (:func:`n8n_enabled`);
otherwise :func:`start_tasks` starts nothing. Every ``n8n_poll_seconds``
(default 120, at least :data:`MIN_INTERVAL_SECONDS`) one round:

1. checks ``/healthz``, lists every workflow and the newest failed executions
   (at most 500, none older than :data:`~robothor.integrations.n8n.store.RETENTION_DAYS`);
2. replaces the workflow snapshot and records the executions it has not seen,
   reading the error message of up to :data:`ERROR_FETCH_CAP` new ones;
3. tells the main agent about every execution not yet alerted: one
   ``workflow_failure`` notification each, or ONE summary when more than
   :data:`COALESCE_ABOVE` are waiting, then stamps them alerted. A
   notification that fails is retried next round; one that succeeded is
   never sent again.

A round that cannot reach n8n (bad key, network, rate limit, no
configuration) records the error class in the sync state and is retried
next round; it never stops the worker or the daemon. After
:data:`OUTAGE_ROUNDS` failed rounds in a row ONE warning alert is raised for
the outage, and a successful round resets it. With HA leader election on,
only the scheduler leader polls, so two replicas never notify twice and a
follower makes no HTTP call at all. Executions older than the retention
window are purged hourly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.integrations.n8n.client import N8nClient, N8nError
from robothor.integrations.n8n.contract import (
    MIN_POLL_SECONDS,
    effective_poll_seconds,
    execution_url,
)
from robothor.integrations.n8n.store import RETENTION_DAYS, N8nStore, PgN8nStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.settings.model import GenusSettings

    Notify = Callable[[str, str, str, dict[str, Any]], Awaitable[Any]]
    Alert = Callable[[str, str, str], Awaitable[Any]]

logger = logging.getLogger(__name__)

__all__ = [
    "COALESCE_ABOVE",
    "ERROR_FETCH_CAP",
    "MIN_INTERVAL_SECONDS",
    "OUTAGE_ROUNDS",
    "client_from_settings",
    "default_notify",
    "n8n_enabled",
    "poll_once",
    "run",
    "start_tasks",
]

MIN_INTERVAL_SECONDS = MIN_POLL_SECONDS
PURGE_EVERY_SECONDS = 3600
#: More new failures than this in one round are summarised in ONE notification.
COALESCE_ABOVE = 5
#: Failed rounds in a row before the outage is alerted (once).
OUTAGE_ROUNDS = 3
#: Error messages read per round; each is one extra request.
ERROR_FETCH_CAP = 20
API_KEY_ENV = "ROBOTHOR_N8N_API_KEY"


def _settings(settings: GenusSettings | None) -> GenusSettings:
    if settings is not None:
        return settings
    from robothor.settings import get_settings

    return get_settings()


def n8n_enabled(settings: GenusSettings | None = None) -> bool:
    """Is the n8n monitor switched on?"""
    return bool(_settings(settings).integrations.n8n_enabled)


def _interval(settings: GenusSettings | None) -> int:
    return effective_poll_seconds(_settings(settings).integrations.n8n_poll_seconds)


def _get_secret(name: str) -> str | None:
    from robothor.secrets import get_secret

    return get_secret(name)


def client_from_settings(
    settings: GenusSettings | None = None,
    *,
    secret: Callable[[str], str | None] = _get_secret,
) -> N8nClient:
    """A client for the configured instance; the key is resolved vault-first.

    Raises ``N8nError("not_configured")`` when the base URL or the key is missing.
    """
    group = _settings(settings).integrations
    base_url = (group.n8n_base_url or "").strip()
    key = secret(API_KEY_ENV) or (group.n8n_api_key or "").strip()
    if not base_url or not key:
        raise N8nError("not_configured")
    return N8nClient(base_url, key)


async def default_notify(tenant_id: str, subject: str, body: str, metadata: dict[str, Any]) -> None:
    """A ``workflow_failure`` row for the main agent, as failed cron workflows write."""
    from robothor.crm import dal

    notif_id = await asyncio.to_thread(
        dal.send_notification,
        from_agent="n8n",
        to_agent="main",
        notification_type="workflow_failure",
        subject=subject,
        body=body,
        metadata=metadata,
        tenant_id=tenant_id,
    )
    if not notif_id:
        raise RuntimeError("the notification row was not written")


async def _default_alert(level: str, title: str, body: str) -> Any:
    from robothor.engine.alerts import alert

    return await alert(level, title, body)


@dataclass
class PollResult:
    ok: bool
    error: str | None = None
    notified: int = 0


#: (subject, body, metadata) of one notification.
Message = tuple[str, str, dict[str, Any]]


def _one(row: dict[str, Any], name: str, base_url: str) -> Message:
    url = execution_url(base_url, row["workflow_id"], row["execution_id"])
    started = row.get("started_at")
    lines = [
        f"n8n workflow '{name}' (id {row['workflow_id']}) failed: "
        f"execution {row['execution_id']}, mode {row.get('mode') or 'unknown'}"
        + (f", started {started.isoformat()}" if isinstance(started, datetime) else "")
        + ".",
    ]
    if row.get("error_message"):
        lines.append(f"Error: {row['error_message']}")
    lines.append(f"Execution: {url}")
    metadata = {
        "source": "n8n",
        "workflow_id": row["workflow_id"],
        "execution_id": row["execution_id"],
        "url": url,
    }
    return f"n8n: {name} failed", "\n".join(lines), metadata


def _summary(rows: list[dict[str, Any]], names: dict[str, str], base_url: str) -> Message:
    per_workflow: dict[str, int] = {}
    for row in rows:
        per_workflow[row["workflow_id"]] = per_workflow.get(row["workflow_id"], 0) + 1
    lines = [f"{len(rows)} n8n workflow executions failed since the last notice:"]
    for wid, count in sorted(per_workflow.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {names.get(wid, wid)} (id {wid}): {count}")
    newest = rows[-1]
    lines.append(
        "Newest: " + execution_url(base_url, newest["workflow_id"], newest["execution_id"])
    )
    metadata = {
        "source": "n8n",
        "count": len(rows),
        "execution_ids": [r["execution_id"] for r in rows],
        "workflow_ids": sorted(per_workflow),
    }
    return f"n8n: {len(rows)} workflow executions failed", "\n".join(lines), metadata


async def _notify_pending(
    tenant_id: str,
    store: N8nStore,
    notify: Notify,
    names: dict[str, str],
    base_url: str,
) -> int:
    pending = await store.pending_alerts(tenant_id)
    if not pending:
        return 0
    if len(pending) > COALESCE_ABOVE:
        subject, body, metadata = _summary(pending, names, base_url)
        try:
            await notify(tenant_id, subject, body, metadata)
        except Exception as exc:  # noqa: BLE001 - retried next round
            logger.warning("n8n failure summary deferred: %s", type(exc).__name__)
            return 0
        await store.mark_alerted(tenant_id, [r["execution_id"] for r in pending])
        return 1
    sent: list[str] = []
    for row in pending:
        name = names.get(row["workflow_id"], row["workflow_id"])
        subject, body, metadata = _one(row, name, base_url)
        try:
            await notify(tenant_id, subject, body, metadata)
        except Exception as exc:  # noqa: BLE001 - retried next round
            logger.warning("n8n failure notification deferred: %s", type(exc).__name__)
            continue
        sent.append(row["execution_id"])
    if sent:
        await store.mark_alerted(tenant_id, sent)
    return len(sent)


async def poll_once(
    tenant_id: str,
    *,
    client: N8nClient,
    store: N8nStore,
    notify: Notify,
    now: datetime,
) -> PollResult:
    """One round against an open client. Raises :class:`N8nError` on an API failure."""
    await client.healthz()
    workflows = await client.list_workflows()
    executions = await client.list_error_executions()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    executions = [
        e
        for e in executions
        if isinstance(e.get("started_at"), datetime) and e["started_at"] >= cutoff
    ]

    await store.upsert_workflows(tenant_id, workflows)
    new_ids = await store.record_executions(tenant_id, executions)
    messages: dict[str, str] = {}
    for eid in new_ids[:ERROR_FETCH_CAP]:
        try:
            message = await client.get_execution_error(eid)
        except N8nError as exc:
            logger.info("n8n: error message for an execution unavailable (%s)", exc.kind)
            continue
        if message:
            messages[eid] = message
    if messages:
        await store.set_error_messages(tenant_id, messages)

    names = {w["workflow_id"]: w["name"] or w["workflow_id"] for w in workflows}
    notified = await _notify_pending(tenant_id, store, notify, names, client.base_url)
    await store.set_sync_state(
        tenant_id,
        polled_at=now,
        ok=True,
        workflows_total=len(workflows),
        workflows_active=sum(1 for w in workflows if w["active"]),
    )
    return PollResult(ok=True, notified=notified)


async def _round(
    tenant_id: str,
    *,
    settings: GenusSettings | None,
    client_factory: Callable[[GenusSettings | None], N8nClient],
    store: N8nStore,
    notify: Notify,
    now: datetime,
) -> PollResult:
    """One round that never raises: an API failure is recorded and reported."""
    try:
        async with client_factory(settings) as client:
            return await poll_once(tenant_id, client=client, store=store, notify=notify, now=now)
    except N8nError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad round must not stop the monitor
        error = type(exc).__name__
    logger.warning("n8n poll deferred: %s", error)
    try:
        await store.set_sync_state(tenant_id, polled_at=now, ok=False, error=error)
    except Exception as exc:  # noqa: BLE001
        logger.warning("n8n sync state not written: %s", type(exc).__name__)
    return PollResult(ok=False, error=error)


async def run(
    tenant_id: str,
    *,
    settings: GenusSettings | None = None,
    client_factory: Callable[[GenusSettings | None], N8nClient] | None = None,
    store: N8nStore | None = None,
    notify: Notify | None = None,
    alert: Alert | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rounds: int | None = None,
    is_leader: Callable[[], bool] | None = None,
    now: Callable[[], datetime] | None = None,
    secret: Callable[[str], str | None] = _get_secret,
) -> None:
    """Poll until cancelled (or for ``rounds`` rounds, in tests).

    Returns at once, having touched nothing, when the monitor is off.
    """
    if not n8n_enabled(settings):
        return
    if client_factory is None:
        client_factory = lambda s: client_from_settings(s, secret=secret)  # noqa: E731

    if store is None:
        store = PgN8nStore()
    if notify is None:
        notify = default_notify
    if alert is None:
        alert = _default_alert
    if is_leader is None:
        from robothor.engine.leader import is_leader as leader

        is_leader = leader

    clock = now or (lambda: datetime.now(UTC))
    failures = 0
    outage_alerted = False
    last_purge: datetime | None = None
    done = 0
    while rounds is None or done < rounds:
        done += 1
        if is_leader():
            current = clock()
            result = await _round(
                tenant_id,
                settings=settings,
                client_factory=client_factory,
                store=store,
                notify=notify,
                now=current,
            )
            if result.ok:
                failures, outage_alerted = 0, False
            else:
                failures += 1
                if failures >= OUTAGE_ROUNDS and not outage_alerted:
                    outage_alerted = True
                    try:
                        await alert(
                            "warning",
                            "n8n monitor: polls failing",
                            f"The last {failures} n8n polls failed ({result.error}). "
                            "Workflow failures are not being reported until it recovers. "
                            "Run `genus doctor --only n8n.connection`.",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("n8n outage alert not sent: %s", type(exc).__name__)
            if last_purge is None or (current - last_purge).total_seconds() >= PURGE_EVERY_SECONDS:
                try:
                    await store.purge(tenant_id, older_than_days=RETENTION_DAYS)
                    last_purge = current
                except Exception as exc:  # noqa: BLE001
                    logger.warning("n8n purge deferred: %s", type(exc).__name__)
        if rounds is None or done < rounds:
            await sleep(_interval(settings))


def start_tasks(tenant_id: str) -> list[asyncio.Task[None]]:
    """The daemon's n8n task: one when the monitor is on, none otherwise.

    A daemon subsystem that returns ends the daemon, so the disabled case
    starts no task rather than one that returns at once.
    """
    if not n8n_enabled():
        return []
    return [asyncio.create_task(run(tenant_id), name="n8n-monitor")]
