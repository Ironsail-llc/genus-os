"""Agent-to-agent messaging — durable Postgres inbox, Redis wake push.

The first fabric was Redis lists with a ONE HOUR TTL and no acknowledgement:
a message to a busy agent silently evaporated if it was not polled within the
hour, an engine restart lost the inbox invisibly, and nothing recorded that a
message ever existed. Production coordination routed around it (CRM tasks by
convention) — the tell that agents could not trust it — and the 2026-08-24
competitive sweep named the ephemeral fabric the one thing eroding an
otherwise-leading multi-agent story.

Now: Postgres is the store (``agent_messages``, tenant-RLS'd like every other
engine table, migration 105) and Redis keeps exactly one job — the real-time
wake publish — because a lost pub/sub ping costs a poll delay, never a
message.

Delivery semantics: ``receive`` claims messages atomically
(``UPDATE … RETURNING`` over the oldest undelivered rows), so a message is
handed out exactly once even with concurrent receivers. Undelivered messages
survive restarts until retention: delivered rows purge after 7 days,
undelivered after 30 (see ``purge_old_messages``).

The public API is unchanged from the Redis era: ``send`` / ``receive`` /
``broadcast`` / ``inbox_count`` / ``AgentMessage`` — callers did not move.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

MSG_CHANNEL_PREFIX = "robothor:msg:"


@dataclass
class AgentMessage:
    """A message between agents."""

    from_agent: str
    to_agent: str
    content: str
    timestamp: float = 0.0
    team_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> AgentMessage:
        return cls(**json.loads(data))


class AgentMessenger:
    """Durable agent-to-agent messaging: Postgres inbox + Redis wake push.

    ``redis_client``: an explicit client, ``None`` to lazily build one from
    config for wake publishes, or ``False`` to disable wake publishes
    entirely (tests, environments without Redis).
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    # ── Wake push (best-effort, never load-bearing) ──────────────────────

    def _get_redis(self) -> Any:
        if self._redis is False:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis

            from robothor.config import get_config

            cfg = get_config()
            self._redis = redis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=cfg.redis.password or None,
            )
            return self._redis
        except Exception as e:
            logger.debug("Redis unavailable for message wake push: %s", e)
            return None

    def _wake(self, msg: AgentMessage) -> None:
        """Publish a wake ping. Failure costs a poll delay, never a message."""
        try:
            r = self._get_redis()
            if r is not None:
                r.publish(f"{MSG_CHANNEL_PREFIX}{msg.to_agent}", msg.to_json())
        except Exception as e:
            logger.debug("Message wake publish failed (message is stored): %s", e)

    # ── Durable API ──────────────────────────────────────────────────────

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        *,
        team_id: str = "",
        metadata: dict[str, Any] | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> bool:
        """Store a message durably, then wake the recipient. True on stored.

        The write is the success criterion — a stored-but-unwoken message is
        delivered on the recipient's next poll, while the old fabric's
        published-but-expired message was simply gone.
        """
        msg = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            team_id=team_id,
            metadata=metadata or {},
        )
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO agent_messages
                       (tenant_id, from_agent, to_agent, content, team_id, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        tenant_id or DEFAULT_TENANT,
                        from_agent,
                        to_agent,
                        content,
                        team_id,
                        json.dumps(metadata or {}),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to store message to %s: %s", to_agent, e)
            return False
        self._wake(msg)
        return True

    def receive(
        self,
        agent_id: str,
        limit: int = 10,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> list[AgentMessage]:
        """Claim and return the oldest undelivered messages, exactly once each.

        The claim is atomic: ``UPDATE … RETURNING`` over a ``FOR UPDATE SKIP
        LOCKED`` selection, so two concurrent receivers can never hand out the
        same message twice.
        """
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """UPDATE agent_messages m
                       SET delivered_at = now()
                       WHERE m.id IN (
                           SELECT id FROM agent_messages
                           WHERE tenant_id = %s AND to_agent = %s
                             AND delivered_at IS NULL
                           ORDER BY created_at
                           LIMIT %s
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING m.from_agent, m.to_agent, m.content,
                                 extract(epoch FROM m.created_at),
                                 m.team_id, m.metadata""",
                    (tenant_id or DEFAULT_TENANT, agent_id, limit),
                )
                rows = cur.fetchall()
                conn.commit()
        except Exception as e:
            logger.error("Failed to receive messages for %s: %s", agent_id, e)
            return []

        messages = []
        for from_agent, to_agent, content, epoch, team_id, metadata in rows:
            messages.append(
                AgentMessage(
                    from_agent=from_agent,
                    to_agent=to_agent,
                    content=content,
                    timestamp=float(epoch),
                    team_id=team_id or "",
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            )
        # RETURNING gives no deterministic order — re-sort to FIFO.
        messages.sort(key=lambda m: m.timestamp)
        return messages

    def broadcast(
        self,
        from_agent: str,
        team_id: str,
        member_ids: list[str],
        content: str,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """Send to every team member except the sender. Returns count sent."""
        sent = 0
        for member_id in member_ids:
            if member_id == from_agent:
                continue
            if self.send(from_agent, member_id, content, team_id=team_id, tenant_id=tenant_id):
                sent += 1
        return sent

    def inbox_count(self, agent_id: str, *, tenant_id: str = DEFAULT_TENANT) -> int:
        """Number of undelivered messages waiting for an agent."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """SELECT count(*) FROM agent_messages
                       WHERE tenant_id = %s AND to_agent = %s AND delivered_at IS NULL""",
                    (tenant_id or DEFAULT_TENANT, agent_id),
                )
                return int(cur.fetchone()[0])
        except Exception:
            return 0


def purge_old_messages(delivered_days: int = 7, undelivered_days: int = 30) -> int:
    """Retention: delivered rows purge earlier than undelivered ones.

    An inbox nobody drains for ``undelivered_days`` is a dead recipient, not a
    mailbox — and the purge LOGS what it drops per recipient, because silently
    deleting undelivered mail is how the old fabric's failure mode sneaks back
    wearing a retention policy.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """DELETE FROM agent_messages
                   WHERE (delivered_at IS NOT NULL
                          AND delivered_at < now() - make_interval(days => %s))
                      OR (delivered_at IS NULL
                          AND created_at < now() - make_interval(days => %s))
                   RETURNING to_agent, delivered_at IS NULL""",
                (delivered_days, undelivered_days),
            )
            rows = cur.fetchall()
            conn.commit()
    except Exception as e:
        logger.warning("Message retention purge failed: %s", e)
        return 0

    undelivered_by_agent: dict[str, int] = {}
    for to_agent, was_undelivered in rows:
        if was_undelivered:
            undelivered_by_agent[to_agent] = undelivered_by_agent.get(to_agent, 0) + 1
    if undelivered_by_agent:
        logger.warning(
            "Message retention dropped UNDELIVERED mail (dead recipients?): %s",
            ", ".join(f"{a}={n}" for a, n in sorted(undelivered_by_agent.items())),
        )
    return len(rows)


# Module-level singleton
_messenger: AgentMessenger | None = None


def get_messenger() -> AgentMessenger | None:
    return _messenger


def init_messenger(redis_client: Any = None) -> AgentMessenger:
    global _messenger
    _messenger = AgentMessenger(redis_client)
    return _messenger
