"""
Genus OS Event Bus — Redis Streams based publish-subscribe.

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
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable

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
_BASE_STREAMS = {"email", "calendar", "crm", "vision", "health", "agent", "system", "channel"}

# Dynamic: extend with ROBOTHOR_EXTRA_STREAMS env var (comma-separated)
_extra = os.environ.get("ROBOTHOR_EXTRA_STREAMS", "")
VALID_STREAMS = _BASE_STREAMS | {s.strip() for s in _extra.split(",") if s.strip()}

# Max stream length per stream (circular buffer)
MAXLEN = int(os.environ.get("EVENT_BUS_MAXLEN", "10000"))

# Redis connection singleton
_redis_client = None

# The destination when nothing is configured — production.
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# The one namespace tests may publish onto. Same default as the ``redis_url``
# fixture in tests/conftest_integration.py, so integration tests that take a
# real Redis and unit tests that go through the bus agree on where "test" is.
TEST_REDIS_URL_ENV = "ROBOTHOR_TEST_REDIS_URL"
DEFAULT_TEST_REDIS_URL = "redis://localhost:6379/15"

# Escape hatch, mirroring ROBOTHOR_TEST_DB_ALLOW in robothor/db/connection.py:
# a comma-separated list of namespaces (``host:port/db``, or a full redis URL)
# that this run may publish onto despite not being the test namespace.
EVENT_BUS_ALLOW_ENV = "ROBOTHOR_EVENT_BUS_ALLOW"


class EventBusGuardError(BaseException):
    """Raised when pytest is about to publish onto a non-test event bus.

    Derived from ``BaseException`` rather than ``Exception``, deliberately.
    Every producer in this codebase wraps its publish in a best-effort
    ``except Exception: logger.warning(...)`` so that a Redis outage cannot
    fail an otherwise good run — ``robothor/crm/dal.py``,
    ``robothor/vision/service.py``, ``robothor/engine/autodream.py``,
    ``channel_bus.py``, ``delivery.py`` and the bridge routers all do. Those
    handlers swallow an ``Exception``-derived guard and downgrade "this test is
    writing to production" to a log line nobody reads, which is exactly how the
    first pass at this guard came to be inert. Verified, not assumed: with an
    ``Exception``-derived guard, aiming tests/test_operator_identity.py at
    production Redis logged ``dal.py:2364 Failed to publish task.resolved
    event`` twice and the run still reported ``20 passed``.

    Sitting outside the ``Exception`` hierarchy means no handler that exists
    today — and none written tomorrow — can catch it by accident, so the check
    does not depend on every call site remembering to re-raise. That is the
    difference between this and
    :class:`robothor.db.connection.DatabaseGuardError`, which stays an
    ``Exception`` because it has to remain catchable by pre-existing
    ``pytest.raises(RuntimeError)`` call sites; it pays for that with two
    hand-written re-raises that a third writer would have to remember.

    :func:`assert_test_event_bus` is a no-op outside pytest, so this type is
    never raised in production and cannot destabilise a live run.
    """


def _in_pytest() -> bool:
    """Whether this process is running under pytest.

    Broader than a bare ``PYTEST_CURRENT_TEST`` check, which is set only during
    a test's setup/call/teardown — it is absent during collection, during
    session-scoped fixture setup, and in threads that outlive a test. Those are
    exactly the windows a stray module-level publish slips through.

    Duplicated rather than imported from :func:`robothor.db.connection.in_pytest`
    (and from ``robothor.engine.model_breaker._in_pytest``, which does the same)
    to keep the event bus free of a psycopg2 import. No production entry point
    imports pytest, so the ``sys.modules`` probe cannot fire live.
    """
    return (
        "PYTEST_CURRENT_TEST" in os.environ
        or "PYTEST_VERSION" in os.environ
        or "pytest" in sys.modules
    )


def _namespace(host: str | None, port: Any, db: Any) -> str:
    """Render a Redis destination as a comparable ``host:port/db`` string."""
    return f"{host or 'localhost'}:{port or 6379}/{db if db is not None else 0}"


def _namespace_from_url(url: str) -> str:
    """The Redis destination a URL points at, as ``host:port/db``.

    Anything that is not a ``redis://``/``rediss://`` URL is returned verbatim:
    a destination this function cannot parse must never normalise into a string
    that happens to match the allowlist.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme not in ("redis", "rediss"):
        return url
    try:
        port = parsed.port
    except ValueError:
        return url
    return _namespace(parsed.hostname, port, (parsed.path or "").lstrip("/") or "0")


def event_bus_namespace(client: Any = None) -> str:
    """The Redis destination that would actually receive a write.

    Authoritative in a way ``REDIS_URL`` is not: :func:`set_redis_client` is
    public, so a caller can inject a client built from any DSN and the
    environment variable then describes nothing. Asking the live client closes
    that hole — the same reason
    :func:`robothor.db.connection.connection_database_name` interrogates the
    connection instead of trusting the resolved config.

    Test doubles have no real connection pool and write nowhere, so they fall
    back to ``REDIS_URL``, which is then the only real destination in play.
    """
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if isinstance(kwargs, dict):
        path = kwargs.get("path")
        if path:
            return f"unix:{path}/{kwargs.get('db', 0)}"
        return _namespace(kwargs.get("host"), kwargs.get("port"), kwargs.get("db", 0))
    return _namespace_from_url(os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))


def _allowed_namespaces() -> set[str]:
    """Namespaces this run may publish onto — a positive allowlist.

    Deliberately not a blocklist of ``db == 0``: a production Redis on another
    host, or any non-zero production database, would sail straight through one.
    """
    allowed = {_namespace_from_url(os.environ.get(TEST_REDIS_URL_ENV, DEFAULT_TEST_REDIS_URL))}
    for entry in os.environ.get(EVENT_BUS_ALLOW_ENV, "").split(","):
        entry = entry.strip()
        if entry:
            allowed.add(_namespace_from_url(entry) if "://" in entry else entry)
    return allowed


def assert_test_event_bus(namespace: str) -> None:
    """Refuse to emit an event onto a non-test Redis from inside pytest.

    Synthetic events published onto a live stream are indistinguishable from
    real ones to every consumer downstream, and the engine's hook pipeline
    treats them as genuine. Between 2026-03-02 and 2026-08-21 the suite put 601
    synthetic ``camera="test-camera"`` payloads into ``robothor:events:vision``
    (44.7% of the stream; 99.6% of everything written to it since June) and
    polluted ``robothor:events:crm`` from tests/test_operator_identity.py.

    Outside pytest this is a no-op. A namespace can be explicitly allowed via
    ``ROBOTHOR_EVENT_BUS_ALLOW``, mirroring ``ROBOTHOR_TEST_DB_ALLOW``.

    Raises:
        EventBusGuardError: under pytest, when ``namespace`` is not allowed.
    """
    if not _in_pytest():
        return
    allowed = _allowed_namespaces()
    if namespace in allowed:
        return
    raise EventBusGuardError(
        f"Refusing to publish an event onto Redis {namespace!r} from inside pytest — "
        f"only {sorted(allowed)} may be published to, and synthetic events written "
        "anywhere else are indistinguishable from real ones to every consumer "
        "downstream (the live engine reads them as genuine hooks). Point REDIS_URL at "
        f"the test namespace ({DEFAULT_TEST_REDIS_URL}), or set "
        f"{EVENT_BUS_ALLOW_ENV}={namespace} to explicitly allow this exact destination."
    )


def _get_redis() -> Any:
    """Get or create Redis connection. Returns None on failure.

    Raises:
        EventBusGuardError: under pytest, when ``REDIS_URL`` names a namespace
            that is not on the test allowlist. The check sits deliberately
            *outside* the ``try`` below, so that no Redis client is ever built
            for an off-allowlist destination — the handler below exists to
            downgrade "Redis is down" to a warning and a ``None`` return, and
            a guard inside it read as indistinguishable from an outage. The
            error's base class makes that placement belt-and-braces rather than
            load-bearing. This is the client-construction half of the guard;
            :func:`publish` checks again at the moment of the write.
    """
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None

    redis_url = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    assert_test_event_bus(_namespace_from_url(redis_url))

    try:
        import redis

        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning("Event bus: Redis connection failed: %s", e)
        _redis_client = None
        return None


def set_redis_client(client: Any) -> None:
    """Override Redis client for testing."""
    global _redis_client
    _redis_client = client


def reset_client() -> None:
    """Reset the Redis client singleton."""
    global _redis_client
    _redis_client = None


def _stream_key(stream: str) -> str:
    """Get full Redis stream key."""
    return f"{STREAM_PREFIX}{stream}"


def _make_envelope(
    event_type: str,
    payload: dict[str, Any],
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
    payload: dict[str, Any],
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
        Never raises in production — failures are logged but non-fatal.

    Raises:
        EventBusGuardError: under pytest only, when the event would land on a
            namespace that is not on the test allowlist.
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

        # The emit boundary, and the check that matters. _get_redis() vetted
        # REDIS_URL, but set_redis_client() is public: a test can inject a
        # client built from any DSN and never touch the env var. Vet the
        # destination that would actually receive the XADD. Every producer in
        # the codebase reaches Redis through this function, so covering it
        # covers them all — including robothor.crm.dal, whose crm.* events
        # tests/test_operator_identity.py was publishing onto production.
        assert_test_event_bus(event_bus_namespace(r))

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
    handler: Callable[[dict[str, Any]], None],
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


def stream_info(stream: str) -> dict[str, Any] | None:
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


def read_recent(stream: str, count: int = 10) -> list[dict[str, Any]]:
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
