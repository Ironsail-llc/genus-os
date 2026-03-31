"""Agent-to-agent real-time messaging via Redis.

Provides direct messaging between agents and team broadcasts.
Messages are stored in Redis lists with TTL for ephemeral inbox,
and published to channels for real-time push.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Redis key prefixes
INBOX_PREFIX = "robothor:inbox:"
MSG_CHANNEL_PREFIX = "robothor:msg:"
MESSAGE_TTL = 3600  # 1 hour


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
    """Real-time agent-to-agent messaging via Redis."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    def _get_redis(self) -> Any:
        """Get or create Redis client."""
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
            logger.error("Failed to connect to Redis for messaging: %s", e)
            return None

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        *,
        team_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Send a message to another agent's inbox.

        Returns True if sent successfully.
        """
        r = self._get_redis()
        if r is None:
            return False

        msg = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            team_id=team_id,
            metadata=metadata or {},
        )

        inbox_key = f"{INBOX_PREFIX}{to_agent}"
        try:
            r.lpush(inbox_key, msg.to_json())
            r.expire(inbox_key, MESSAGE_TTL)
            # Also publish for real-time listeners
            r.publish(f"{MSG_CHANNEL_PREFIX}{to_agent}", msg.to_json())
            return True
        except Exception as e:
            logger.error("Failed to send message to %s: %s", to_agent, e)
            return False

    def receive(self, agent_id: str, limit: int = 10) -> list[AgentMessage]:
        """Receive messages from agent's inbox (FIFO)."""
        r = self._get_redis()
        if r is None:
            return []

        inbox_key = f"{INBOX_PREFIX}{agent_id}"
        messages = []
        try:
            for _ in range(limit):
                raw = r.rpop(inbox_key)
                if raw is None:
                    break
                data = raw.decode() if isinstance(raw, bytes) else raw
                messages.append(AgentMessage.from_json(data))
        except Exception as e:
            logger.error("Failed to receive messages for %s: %s", agent_id, e)

        return messages

    def broadcast(
        self,
        from_agent: str,
        team_id: str,
        member_ids: list[str],
        content: str,
    ) -> int:
        """Broadcast a message to all team members (except sender).

        Returns count of messages sent.
        """
        sent = 0
        for member_id in member_ids:
            if member_id == from_agent:
                continue
            if self.send(from_agent, member_id, content, team_id=team_id):
                sent += 1
        return sent

    def inbox_count(self, agent_id: str) -> int:
        """Get the number of pending messages in an agent's inbox."""
        r = self._get_redis()
        if r is None:
            return 0
        try:
            return r.llen(f"{INBOX_PREFIX}{agent_id}") or 0
        except Exception:
            return 0


# Module-level singleton
_messenger: AgentMessenger | None = None


def get_messenger() -> AgentMessenger | None:
    return _messenger


def init_messenger(redis_client: Any = None) -> AgentMessenger:
    global _messenger
    _messenger = AgentMessenger(redis_client)
    return _messenger
