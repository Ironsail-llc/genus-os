"""The daemon worker that runs the Microsoft 365 ingestors.

Enabled ONLY when ``workspace_provider = microsoft365``
(:func:`ingest_enabled`). On a Google instance :func:`start_tasks` starts
nothing: there the instance's own sync scripts publish ``email.new`` and
``calendar.*``, and a second producer would publish every message twice.

Every ``m365_ingest_interval_seconds`` (default 60) one round runs the
assistant-inbox ingestor and, when ``m365_owner_mailbox`` is set, the
owner-calendar ingestor. A failing ingestor is logged and retried next round;
it never stops the other one or the daemon. With HA leader election on, only
the scheduler leader ingests, so two replicas never race to publish the same
message. Seen ids older than
:data:`~robothor.workspace.ingest.state.SEEN_TTL` are purged hourly.

As the Google sync script does, each new email is also logged to the CRM as
an interaction (:mod:`~robothor.workspace.ingest.crm`, once per message), and
``triage-inbox.json`` is rebuilt (:mod:`~robothor.workspace.ingest.triage`)
right after a round that brought new mail and otherwise every
:data:`TRIAGE_REFRESH_SECONDS`, the script's cadence. A rebuild with items
publishes ``triage.refreshed``, which the email classifier hooks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.events import contract
from robothor.workspace.ingest.base import Ingestor, IngestReport, Publisher, bus_publisher
from robothor.workspace.ingest.state import SEEN_TTL, IngestStore, PgIngestStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.settings.model import GenusSettings
    from robothor.workspace.ingest.crm import InteractionLogger
    from robothor.workspace.ingest.email_log import EmailLog
    from robothor.workspace.ingest.triage import TriageInbox
    from robothor.workspace.microsoft.graph import GraphClient

logger = logging.getLogger(__name__)

__all__ = [
    "TRIAGE_REFRESH_SECONDS",
    "TriageRefresher",
    "build_ingestors",
    "ingest_enabled",
    "run",
    "run_round",
    "start_tasks",
]

PURGE_EVERY_SECONDS = 3600
MIN_INTERVAL_SECONDS = 10
#: The triage inbox is rebuilt at least this often (the Google sync's 5-minute cron).
TRIAGE_REFRESH_SECONDS = 300


def _settings(settings: GenusSettings | None) -> GenusSettings:
    if settings is not None:
        return settings
    from robothor.settings import get_settings

    return get_settings()


def ingest_enabled(settings: GenusSettings | None = None) -> bool:
    """Does this instance ingest Microsoft 365? Only under ``workspace_provider=microsoft365``."""
    return _settings(settings).workspace.workspace_provider == "microsoft365"


def build_ingestors(
    tenant_id: str,
    *,
    graph: GraphClient,
    store: IngestStore,
    publish: Publisher,
    email_log: EmailLog | None,
    log_interaction: InteractionLogger | None = None,
    settings: GenusSettings | None = None,
    now: Callable[[], datetime] | None = None,
) -> list[Ingestor]:
    """The ingestors the settings call for: the assistant inbox, then the owner calendar."""
    from robothor.workspace.ingest.microsoft import GraphCalendarIngestor, GraphMailIngestor

    ws = _settings(settings).workspace
    common: dict[str, Any] = {
        "graph": graph,
        "tenant_id": tenant_id,
        "store": store,
        "publish": publish,
        "now": now,
    }
    ingestors: list[Ingestor] = []
    assistant = (ws.m365_assistant_mailbox or "").strip()
    owner = (ws.m365_owner_mailbox or "").strip()
    if assistant:
        ingestors.append(
            GraphMailIngestor(
                mailbox=assistant, email_log=email_log, log_interaction=log_interaction, **common
            )
        )
    if owner:
        ingestors.append(GraphCalendarIngestor(mailbox=owner, **common))
    return ingestors


async def run_round(ingestors: list[Ingestor]) -> list[IngestReport]:
    """One pass over every ingestor; the reports of those that finished.

    Failures are logged by type, never raised.
    """
    reports: list[IngestReport] = []
    for ingestor in ingestors:
        try:
            report = await ingestor.run_once()
        except Exception as exc:  # noqa: BLE001 - one resource failing must not stop the rest
            from robothor.workspace.errors import WorkspaceError

            detail = str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
            logger.warning("microsoft365 %s ingest deferred: %s", ingestor.resource, detail)
            continue
        reports.append(report)
        if report.published or report.held_back or report.mode != "incremental":
            logger.info(
                "microsoft365 %s ingest (%s): read %d, published %d, held back %d, crm %d",
                report.resource,
                report.mode,
                report.read,
                report.published,
                report.held_back,
                report.crm_logged,
            )
    return reports


class TriageRefresher:
    """Rebuilds the triage inbox after new mail, else every :data:`TRIAGE_REFRESH_SECONDS`."""

    def __init__(self, triage: TriageInbox, publish: Publisher) -> None:
        self.triage = triage
        self.publish = publish
        self.last: datetime | None = None

    async def after_round(self, reports: list[IngestReport], now: datetime) -> None:
        new_mail = any(r.resource == "mail" and r.published for r in reports)
        due = self.last is None or (now - self.last).total_seconds() >= TRIAGE_REFRESH_SECONDS
        if not (new_mail or due):
            return
        try:
            inbox = await self.triage.rebuild(now=now)
        except Exception as exc:  # noqa: BLE001 - the triage file must never stop ingestion
            logger.warning("microsoft365 triage inbox deferred: %s", type(exc).__name__)
            return
        self.last = now
        counts = inbox.get("counts") or {}
        total = int(counts.get("total") or 0)
        if not total:
            return
        try:
            await self.publish(
                contract.EMAIL_STREAM,
                contract.TRIAGE_REFRESHED,
                {"total": total, "emails": int(counts.get("emails") or 0)},
            )
        except Exception as exc:  # noqa: BLE001 - the next rebuild announces it again
            logger.warning("microsoft365 triage.refreshed deferred: %s", type(exc).__name__)


async def run(
    tenant_id: str,
    *,
    settings: GenusSettings | None = None,
    graph_factory: Callable[[str], Awaitable[GraphClient]] | None = None,
    store: IngestStore | None = None,
    publish: Publisher | None = None,
    email_log: EmailLog | None = None,
    log_interaction: InteractionLogger | None = None,
    triage: TriageInbox | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rounds: int | None = None,
    now: Callable[[], datetime] | None = None,
    leader: Callable[[], bool] | None = None,
) -> None:
    """Ingest until cancelled (or for ``rounds`` rounds, in tests).

    Returns at once, having touched nothing, when the provider is not
    microsoft365.
    """
    if not ingest_enabled(settings):
        return
    if graph_factory is None:
        from robothor.workspace.microsoft import graph_client_from_vault

        graph_factory = graph_client_from_vault
    if store is None:
        store = PgIngestStore()
    if publish is None:
        publish = bus_publisher(tenant_id)
    if email_log is None:
        from robothor.workspace.ingest.email_log import EmailLog

        email_log = EmailLog()
    if log_interaction is None:
        from robothor.workspace.ingest.crm import dal_interaction_logger

        log_interaction = dal_interaction_logger(tenant_id)
    if triage is None:
        from robothor.workspace.ingest import triage as triage_mod

        triage = triage_mod.TriageInbox(
            triage_mod.default_path(email_log.path),
            email_log_path=email_log.path,
            escalations=triage_mod.pg_escalation_ids(tenant_id),
        )
    refresher = TriageRefresher(triage, publish)
    if leader is None:
        from robothor.engine.leader import is_leader

        leader = is_leader
    clock = now or (lambda: datetime.now(UTC))
    interval = max(
        MIN_INTERVAL_SECONDS, int(_settings(settings).workspace.m365_ingest_interval_seconds)
    )

    graph: GraphClient | None = None
    ingestors: list[Ingestor] = []
    last_purge: datetime | None = None
    done = 0
    try:
        while rounds is None or done < rounds:
            done += 1
            if not leader():
                if rounds is None or done < rounds:
                    await sleep(interval)
                continue
            if graph is None:
                try:
                    graph = await graph_factory(tenant_id)
                except Exception as exc:  # noqa: BLE001 - "not connected" is retried, not fatal
                    from robothor.workspace.errors import WorkspaceError

                    detail = str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                    logger.warning("microsoft365 ingest waiting for a credential: %s", detail)
                if graph is not None:
                    ingestors = build_ingestors(
                        tenant_id,
                        graph=graph,
                        store=store,
                        publish=publish,
                        email_log=email_log,
                        log_interaction=log_interaction,
                        settings=settings,
                        now=now,
                    )
            current = clock()
            if ingestors:
                reports = await run_round(ingestors)
                await refresher.after_round(reports, current)
            if last_purge is None or (current - last_purge).total_seconds() >= PURGE_EVERY_SECONDS:
                try:
                    await store.purge_seen(tenant_id, current - SEEN_TTL)
                    last_purge = current
                except Exception as exc:  # noqa: BLE001
                    logger.warning("microsoft365 ingest purge deferred: %s", type(exc).__name__)
            if rounds is None or done < rounds:
                await sleep(interval)
    finally:
        if graph is not None:
            await graph.aclose()


def start_tasks(tenant_id: str) -> list[asyncio.Task[None]]:
    """The daemon's ingest tasks: one on microsoft365, none on any other provider.

    A daemon subsystem that returns ends the daemon, so the disabled case
    starts no task rather than one that returns at once.
    """
    if not ingest_enabled():
        return []
    return [asyncio.create_task(run(tenant_id), name="m365-ingest")]
