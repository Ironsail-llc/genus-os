"""Gmail push: Pub/Sub tells us the inbox changed, one history round follows.

The flow, end to end:

1. Gmail's ``users.watch`` publishes a notification to a Pub/Sub topic
   whenever the assistant's inbox changes. :func:`run` calls it at start and
   every :data:`WATCH_RENEW_SECONDS` (a watch lapses after seven days), when
   the ``gmail_push_topic`` setting names the topic.
2. A Pub/Sub push subscription POSTs each notification to the engine's
   ``google_oidc`` webhook channel (:mod:`robothor.engine.webhooks`), which
   verifies the push's ID token and publishes ``{emailAddress, historyId}``
   to the channel's stream (``gmail_push_stream``, default ``gmail``).
3. :class:`RedisPushSource` reads that stream as consumer group
   :data:`CONSUMER_GROUP`; a push for the configured mailbox wakes the
   :class:`RoundCoalescer`, which runs one
   :class:`~robothor.workspace.ingest.google.GmailHistoryIngestor` round.

A burst of pushes costs at most two rounds: the one in flight and one more,
which reads everything the burst announced. The notification's ``historyId``
is not used: the ingestor's saved cursor is the source of truth, so a lost or
reordered push is caught up by the next one.

Leader-only, like the Microsoft 365 worker: with HA leader election on, only
the scheduler leader watches, reads the stream and ingests. Off unless
``workspace_provider = google`` and ``gmail_push_enabled``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from robothor.workspace.ingest.google import GmailHistoryApi, GmailHistoryIngestor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.settings.model import GenusSettings
    from robothor.workspace.ingest.base import Publisher
    from robothor.workspace.ingest.crm import InteractionLogger
    from robothor.workspace.ingest.email_log import EmailLog
    from robothor.workspace.ingest.state import IngestStore

logger = logging.getLogger(__name__)

__all__ = [
    "CONSUMER_GROUP",
    "WATCH_LABELS",
    "WATCH_RENEW_SECONDS",
    "PushSource",
    "RedisPushSource",
    "RoundCoalescer",
    "push_enabled",
    "push_matches",
    "renew_watch",
    "run",
    "run_from_settings",
]

#: Must match the webhook and hook stream prefix.
STREAM_PREFIX = "robothor:events:"
CONSUMER_GROUP = "gmail-push"
WATCH_LABELS = ["INBOX"]
#: How often the watch is renewed. Gmail lets one lapse after seven days.
WATCH_RENEW_SECONDS = 86400
#: How soon a failed watch call is tried again.
WATCH_RETRY_SECONDS = 600
#: How long a follower waits before asking whether it leads.
FOLLOWER_SLEEP_SECONDS = 10
#: How long a failed stream read waits before the next.
READ_RETRY_SECONDS = 5
READ_BLOCK_MS = 5000


class RoundCoalescer:
    """Runs ``run_round`` once per wake-up, however many triggers arrived.

    A trigger during a round queues exactly one more; triggers never pile up.
    A failing round is logged by type and does not stop the next.
    """

    def __init__(self, run_round: Callable[[], Awaitable[Any]]) -> None:
        self._run = run_round
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    def trigger(self) -> None:
        self._idle.clear()
        self._wake.set()

    async def serve(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._run()
            except Exception as exc:  # noqa: BLE001 - the next push retries the round
                logger.warning("gmail push ingest round deferred: %s", _detail(exc))
            if not self._wake.is_set():
                self._idle.set()

    async def settle(self) -> None:
        """Wait until no round is running or pending."""
        await self._idle.wait()


def _detail(exc: BaseException) -> str:
    from robothor.workspace.errors import WorkspaceError

    return str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__


def push_matches(payload: dict[str, Any], mailbox: str) -> bool:
    """Is this Gmail notification for ``mailbox``?"""
    address = str(payload.get("emailAddress") or "").strip().lower()
    return bool(address) and address == (mailbox or "").strip().lower()


class PushSource(Protocol):
    async def read(self) -> list[dict[str, Any]]:
        """The push payloads that arrived since the last read (may block briefly)."""
        ...


class RedisPushSource:
    """The webhook channel's stream, read as consumer group :data:`CONSUMER_GROUP`.

    Every entry is acknowledged once read: a push only says "look again", and
    the next round reads whatever a lost one would have announced.
    """

    def __init__(
        self,
        redis: Any,
        *,
        stream: str,
        consumer: str = "engine",
        block_ms: int = READ_BLOCK_MS,
    ) -> None:
        self.redis = redis
        self.key = f"{STREAM_PREFIX}{stream}"
        self.consumer = consumer
        self.block_ms = block_ms
        self._group_ready = False

    async def read(self) -> list[dict[str, Any]]:
        if not self._group_ready:
            with contextlib.suppress(Exception):  # BUSYGROUP: it already exists
                await self.redis.xgroup_create(self.key, CONSUMER_GROUP, id="$", mkstream=True)
            self._group_ready = True
        results = await self.redis.xreadgroup(
            CONSUMER_GROUP, self.consumer, {self.key: ">"}, count=50, block=self.block_ms
        )
        payloads: list[dict[str, Any]] = []
        ids: list[Any] = []
        for _stream, entries in results or []:
            for entry_id, data in entries:
                ids.append(entry_id)
                raw = data.get(b"payload", data.get("payload")) if isinstance(data, dict) else None
                try:
                    payload = json.loads(raw) if raw else None
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    payloads.append(payload)
        if ids:
            await self.redis.xack(self.key, CONSUMER_GROUP, *ids)
        return payloads


async def renew_watch(gmail: GmailHistoryApi, topic: str) -> datetime | None:
    """Call ``users.watch`` for the inbox on ``topic``; the expiration Gmail reports."""
    answer = await gmail.watch(topic, list(WATCH_LABELS))
    try:
        expires = datetime.fromtimestamp(int(str(answer.get("expiration"))) / 1000, tz=UTC)
    except (TypeError, ValueError):
        expires = None
    logger.info(
        "gmail watch renewed; expires %s", expires.isoformat() if expires else "(not reported)"
    )
    return expires


async def run(
    tenant_id: str,
    *,
    mailbox: str,
    topic: str,
    gmail: GmailHistoryApi,
    store: IngestStore,
    publish: Publisher,
    source: PushSource,
    leader: Callable[[], bool],
    email_log: EmailLog | None = None,
    log_interaction: InteractionLogger | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] | None = None,
    rounds: int | None = None,
) -> None:
    """Watch, then ingest on every push, until cancelled (or ``rounds`` reads, in tests)."""
    ingestor = GmailHistoryIngestor(
        gmail=gmail,
        mailbox=mailbox,
        tenant_id=tenant_id,
        store=store,
        publish=publish,
        email_log=email_log,
        log_interaction=log_interaction,
    )
    now = clock or (lambda: datetime.now(UTC))

    async def one_round() -> None:
        report = await ingestor.run_once()
        if report.published or report.mode != "incremental":
            logger.info(
                "gmail push ingest (%s): read %d, published %d, crm %d",
                report.mode,
                report.read,
                report.published,
                report.crm_logged,
            )

    async def watch() -> timedelta:
        try:
            await renew_watch(gmail, topic)
        except Exception as exc:  # noqa: BLE001 - retried, never fatal
            logger.warning("gmail watch renewal deferred: %s", _detail(exc))
            return timedelta(seconds=WATCH_RETRY_SECONDS)
        return timedelta(seconds=WATCH_RENEW_SECONDS)

    coalescer = RoundCoalescer(one_round)
    server = asyncio.create_task(coalescer.serve(), name="gmail-push-rounds")
    started = False
    next_watch: datetime | None = None
    done = 0
    try:
        while rounds is None or done < rounds:
            done += 1
            if not leader():
                await sleep(FOLLOWER_SLEEP_SECONDS)
                continue
            if not started:
                started = True
                if topic:
                    next_watch = now() + await watch()
                try:
                    await one_round()  # the cursor exists before the first push is read
                except Exception as exc:  # noqa: BLE001
                    logger.warning("gmail push ingest round deferred: %s", _detail(exc))
            try:
                payloads = await source.read()
            except Exception as exc:  # noqa: BLE001 - Redis away: wait and read again
                logger.warning("gmail push stream read deferred: %s", type(exc).__name__)
                payloads = []
                await sleep(READ_RETRY_SECONDS)
            if any(push_matches(p, mailbox) for p in payloads):
                coalescer.trigger()
            if topic and next_watch is not None and now() >= next_watch:
                next_watch = now() + await watch()
                coalescer.trigger()  # a reconciliation round with each renewal
        await coalescer.settle()
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server


def _settings(settings: GenusSettings | None) -> GenusSettings:
    if settings is not None:
        return settings
    from robothor.settings import get_settings

    return get_settings()


def push_enabled(settings: GenusSettings | None = None) -> bool:
    """Gmail push ingest: only under ``workspace_provider=google`` with the flag on."""
    ws = _settings(settings).workspace
    return ws.workspace_provider == "google" and bool(ws.gmail_push_enabled)


async def _redis() -> Any:
    import redis.asyncio as aioredis

    from robothor.config import get_config

    cfg = get_config()
    client = aioredis.Redis(
        host=cfg.redis.host,
        port=cfg.redis.port,
        db=cfg.redis.db,
        password=cfg.redis.password or None,
        socket_connect_timeout=5,
    )
    await client.ping()  # type: ignore[misc,unused-ignore]
    return client


async def run_from_settings(
    tenant_id: str,
    *,
    settings: GenusSettings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """:func:`run` with the daemon's collaborators, from the ``gmail_push_*`` settings.

    Waits (retrying every minute) for Redis and, when ``gmail_push_mailbox``
    is empty, for Gmail's profile to name the signed-in address.
    """
    from robothor.engine.leader import is_leader
    from robothor.workspace.ingest.base import bus_publisher
    from robothor.workspace.ingest.crm import dal_interaction_logger
    from robothor.workspace.ingest.email_log import EmailLog
    from robothor.workspace.ingest.google import GwsGmailHistory
    from robothor.workspace.ingest.state import PgIngestStore

    ws = _settings(settings).workspace
    gmail = GwsGmailHistory()
    mailbox = (ws.gmail_push_mailbox or "").strip()
    redis: Any = None
    while not mailbox or redis is None:
        try:
            if not mailbox:
                mailbox = str((await gmail.profile()).get("emailAddress") or "").strip()
            if redis is None:
                redis = await _redis()
        except Exception as exc:  # noqa: BLE001 - "not yet" is retried, not fatal
            logger.warning("gmail push waiting to start: %s", _detail(exc))
        if not mailbox or redis is None:
            await sleep(60)
    if not ws.gmail_push_topic:
        logger.warning("gmail push: no gmail_push_topic set; the watch must be renewed elsewhere")
    try:
        await run(
            tenant_id,
            mailbox=mailbox,
            topic=(ws.gmail_push_topic or "").strip(),
            gmail=gmail,
            store=PgIngestStore(),
            publish=bus_publisher(tenant_id),
            source=RedisPushSource(
                redis, stream=ws.gmail_push_stream or "gmail", consumer=f"engine-{tenant_id}"
            ),
            leader=is_leader,
            email_log=EmailLog(),
            log_interaction=dal_interaction_logger(tenant_id),
            sleep=sleep,
        )
    finally:
        with contextlib.suppress(Exception):
            await redis.aclose()
