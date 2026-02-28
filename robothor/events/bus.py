"""
Robothor Event Bus — Redis Streams based publish-subscribe.

Replaces JSON file polling with real-time event delivery.
Dual-write mode: events go to Redis AND JSON files as fallback.

Streams:
  robothor:events:email     — email sync events
  robothor:events:calendar  — calendar sync events
  robothor:events:crm       — CRM mutations (create, update, delete, merge)
  robothor:events:vision    — vision detection events
  robothor:events:health    — health check results
  robothor:events:agent     — agent actions (hook pipeline, triage, etc.)
  robothor:events:system    — system lifecycle (boot, shutdown, errors)

Envelope format:
  {
    "id": "<stream message ID>",
    "timestamp": "ISO 8601",
    "type": "<event_type>",
    "source": "<producing script/service>",
    "actor": "<agent or system>",
    "payload": "<JSON string>",
    "correlation_id": "<optional trace ID>"
  }

Usage:
    from robothor.events.bus import publish, subscribe, ack

    # Publish
    msg_id = publish("email", "email.new", {"subject": "Hello"}, source="email_sync")

    # Subscribe (blocking consumer loop)
    def handler(event):
        print(event["type"], event["payload"])
    subscribe("email", "triage-group", "triage-worker-1", handler=handler)
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Feature flag — can be disabled to fall back to JSON-only
EVENT_BUS_ENABLED = os.environ.get("EVENT_BUS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Stream prefix
STREAM_PREFIX = "robothor:events:"

# Base stream names (always valid)
_BASE_STREAMS = {"email", "calendar", "crm", "vision", "health", "agent", "system"}

# Dynamic: extend with ROBOTHOR_EXTRA_STREAMS env var (comma-separated)
_extra = os.environ.get("ROBOTHOR_EXTRA_STREAMS", "")
VALID_STREAMS = _BASE_STREAMS | {s.strip() for s in _extra.split(",") if s.strip()}

# Max stream length per stream (circular buffer)
MAXLEN = int(os.environ.get("EVENT_BUS_MAXLEN", "10000"))

# Redis connection singleton
_redis_client = None


def _get_redis():
    """Get or create Redis connection. Returns None on failure."""
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None

    try:
        import redis

        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning("Event bus: Redis connection failed: %s", e)
        _redis_client = None
        return None


def set_redis_client(client):
    """Override Redis client for testing."""
    global _redis_client
    _redis_client = client


def reset_client():
    """Reset the Redis client singleton."""
    global _redis_client
    _redis_client = None


def _stream_key(stream: str) -> str:
    """Get full Redis stream key."""
    return f"{STREAM_PREFIX}{stream}"


def _make_envelope(
    event_type: str,
    payload: dict,
    *,
    source: str = "unknown",
    actor: str = "robothor",
    correlation_id: str | None = None,
    tenant_id: str = "",
) -> dict[str, str]:
    """Create a standardized event envelope for Redis Streams."""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": event_type,
        "source": source,
        "actor": actor,
        "payload": json.dumps(payload) if isinstance(payload, dict) else str(payload),
        "correlation_id": correlation_id or "",
        "tenant_id": tenant_id,
    }


def publish(
    stream: str,
    event_type: str,
    payload: dict,
    *,
    source: str = "unknown",
    actor: str = "robothor",
    correlation_id: str | None = None,
    agent_id: str | None = None,
    tenant_id: str = "",
) -> str | None:
    """Publish an event to a Redis Stream.

    Args:
        stream: Stream name (email, calendar, crm, vision, health, agent, system)
        event_type: Event type string (e.g., "email.new", "crm.create")
        payload: Event payload dict
        source: Producing script/service name
        actor: Agent or system identity
        correlation_id: Optional trace ID for correlation
        agent_id: Agent identity for RBAC check (None = no check, backward compat)
        tenant_id: Tenant identifier for multi-tenant filtering

    Returns:
        Stream message ID on success, None on failure.
        Never raises — failures are logged but non-fatal.
    """
    if not EVENT_BUS_ENABLED:
        return None

    # RBAC enforcement: check stream write access if agent_id is provided
    if agent_id is not None:
        try:
            from robothor.events.capabilities import check_stream_access

            if not check_stream_access(agent_id, stream, "write"):
                logger.warning(
                    "Event bus: agent '%s' denied write access to stream '%s'",
                    agent_id,
                    stream,
                )
                return None
        except ImportError:
            pass  # capabilities module not available — allow (backward compat)

    if stream not in VALID_STREAMS:
        logger.warning(
            "Event bus: stream '%s' not in VALID_STREAMS %s — publishing anyway",
            stream,
            VALID_STREAMS,
        )

    try:
        r = _get_redis()
        if r is None:
            return None

        envelope = _make_envelope(
            event_type,
            payload,
            source=source,
            actor=actor,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
        )
        key = _stream_key(stream)
        msg_id: str | None = r.xadd(key, envelope, maxlen=MAXLEN, approximate=True)
        return msg_id
    except Exception as e:
        logger.warning("Event bus publish failed: %s", e)
        return None


def subscribe(
    stream: str,
    group: str,
    consumer: str,
    *,
    handler: Callable[[dict], None],
    batch_size: int = 10,
    block_ms: int = 5000,
    max_iterations: int | None = None,
    agent_id: str | None = None,
) -> None:
    """Subscribe to a Redis Stream as a consumer group member.

    Creates the consumer group if it doesn't exist.
    Blocks and processes events in a loop.

    Args:
        stream: Stream name
        group: Consumer group name
        consumer: Consumer name within the group
        handler: Callback function receiving parsed event dicts
        batch_size: Number of messages to read per iteration
        block_ms: How long to block waiting for new messages (ms)
        max_iterations: Stop after N iterations (None = infinite, for testing)
        agent_id: Agent identity for RBAC check (None = no check, backward compat)
    """
    if not EVENT_BUS_ENABLED:
        return

    # RBAC enforcement: check stream access if agent_id is provided
    if agent_id is not None:
        try:
            from robothor.events.capabilities import check_stream_access

            if not check_stream_access(agent_id, stream, "read"):
                logger.warning(
                    "Event bus: agent '%s' denied read access to stream '%s'",
                    agent_id,
                    stream,
                )
                return
        except ImportError:
            pass  # capabilities module not available — allow (backward compat)

    r = _get_redis()
    if r is None:
        logger.warning("Event bus: cannot subscribe, Redis unavailable")
        return

    key = _stream_key(stream)

    # Create consumer group if needed
    try:
        r.xgroup_create(key, group, id="0", mkstream=True)
    except Exception as e:
        # Group already exists — this is fine
        if "BUSYGROUP" not in str(e):
            logger.warning("Event bus: failed to create group %s: %s", group, e)

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        try:
            messages = r.xreadgroup(
                group,
                consumer,
                {key: ">"},
                count=batch_size,
                block=block_ms,
            )
            if not messages:
                continue

            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    try:
                        event = {
                            "id": msg_id,
                            "timestamp": fields.get("timestamp", ""),
                            "type": fields.get("type", ""),
                            "source": fields.get("source", ""),
                            "actor": fields.get("actor", ""),
                            "payload": json.loads(fields.get("payload", "{}")),
                            "correlation_id": fields.get("correlation_id", ""),
                            "tenant_id": fields.get("tenant_id", ""),
                        }
                        handler(event)
                        # Auto-ack on successful processing
                        r.xack(key, group, msg_id)
                    except Exception as e:
                        logger.error("Event bus: handler error for %s: %s", msg_id, e)
        except Exception as e:
            logger.warning("Event bus: subscribe loop error: %s", e)
            if max_iterations is not None:
                break
            time.sleep(1)  # Back off on error


def ack(stream: str, group: str, message_id: str) -> bool:
    """Manually acknowledge a message.

    Use this for manual ack mode (when auto-ack is disabled).
    Returns True on success.
    """
    try:
        r = _get_redis()
        if r is None:
            return False
        return bool(r.xack(_stream_key(stream), group, message_id))
    except Exception as e:
        logger.warning("Event bus ack failed: %s", e)
        return False


def stream_length(stream: str) -> int:
    """Get the number of entries in a stream. Returns 0 on error."""
    try:
        r = _get_redis()
        if r is None:
            return 0
        length: int = r.xlen(_stream_key(stream))
        return length
    except Exception as e:
        logger.warning("Event bus stream_length failed: %s", e)
        return 0


def stream_info(stream: str) -> dict | None:
    """Get info about a stream (length, groups, first/last entry)."""
    try:
        r = _get_redis()
        if r is None:
            return None
        info = r.xinfo_stream(_stream_key(stream))
        return {
            "length": info.get("length", 0),
            "first_entry": info.get("first-entry"),
            "last_entry": info.get("last-entry"),
            "groups": info.get("groups", 0),
        }
    except Exception as e:
        logger.warning("Event bus stream_info failed: %s", e)
        return None


def read_recent(stream: str, count: int = 10) -> list[dict]:
    """Read the most recent N entries from a stream (no consumer group).

    Useful for dashboards and monitoring.
    """
    try:
        r = _get_redis()
        if r is None:
            return []
        key = _stream_key(stream)
        entries = r.xrevrange(key, count=count)
        result = []
        for msg_id, fields in entries:
            result.append(
                {
                    "id": msg_id,
                    "timestamp": fields.get("timestamp", ""),
                    "type": fields.get("type", ""),
                    "source": fields.get("source", ""),
                    "actor": fields.get("actor", ""),
                    "payload": json.loads(fields.get("payload", "{}")),
                    "correlation_id": fields.get("correlation_id", ""),
                    "tenant_id": fields.get("tenant_id", ""),
                }
            )
        return result
    except Exception as e:
        logger.warning("Event bus read_recent failed: %s", e)
        return []


def cleanup_stream(stream: str) -> bool:
    """Delete a stream entirely. Use for testing cleanup."""
    try:
        r = _get_redis()
        if r is None:
            return False
        r.delete(_stream_key(stream))
        return True
    except Exception:
        return False
