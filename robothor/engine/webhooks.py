"""Webhook ingress — receive external events and publish to Redis event bus.

External services POST to /api/webhooks/{channel} with an HMAC-SHA256 signature.
The payload is normalized and published to the appropriate Redis stream.
Agents pick up events via the existing EventHooks system.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Must match hooks.py / event_bus.py
STREAM_PREFIX = "robothor:events:"


@dataclass
class WebhookChannel:
    """Configuration for a single webhook channel."""

    name: str
    stream: str
    secret_env: str
    event_type_header: str = ""
    event_type_field: str = ""
    event_type_prefix: str = ""
    enabled: bool = True
    rate_limit_per_min: int = 60


@dataclass
class WebhookConfig:
    """Top-level webhook configuration."""

    channels: dict[str, WebhookChannel] = field(default_factory=dict)


def load_webhook_config(path: str | Path) -> WebhookConfig:
    """Load webhook channel definitions from a YAML file."""
    path = Path(path)
    if not path.exists():
        logger.warning("Webhook config not found: %s", path)
        return WebhookConfig()

    with path.open() as f:
        data = yaml.safe_load(f) or {}

    channels: dict[str, WebhookChannel] = {}
    for name, raw in (data.get("channels") or {}).items():
        if not isinstance(raw, dict):
            continue
        channels[name] = WebhookChannel(
            name=name,
            stream=raw.get("stream", name),
            secret_env=raw.get("secret_env", ""),
            event_type_header=raw.get("event_type_header", ""),
            event_type_field=raw.get("event_type_field", ""),
            event_type_prefix=raw.get("event_type_prefix", ""),
            enabled=raw.get("enabled", True),
            rate_limit_per_min=int(raw.get("rate_limit_per_min", 60)),
        )

    return WebhookConfig(channels=channels)


def _verify_hmac(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature.

    Supports both ``sha256=XXXX`` format (GitHub) and raw hex.
    """
    if not signature or not secret:
        return False

    # Strip "sha256=" prefix if present (GitHub format)
    signature = signature.removeprefix("sha256=")

    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# ── Channel stats (in-memory, reset on restart) ────────────────────

_channel_stats: dict[str, dict[str, Any]] = {}


def _record_received(channel_name: str) -> None:
    """Record that a webhook was received for a channel."""
    now = time.time()
    if channel_name not in _channel_stats:
        _channel_stats[channel_name] = {"total_received": 0, "last_received_at": None}
    _channel_stats[channel_name]["total_received"] += 1
    _channel_stats[channel_name]["last_received_at"] = now


def _get_stats(channel_name: str) -> dict[str, Any]:
    """Get stats for a channel."""
    return _channel_stats.get(channel_name, {"total_received": 0, "last_received_at": None})


# ── Rate limiting ──────────────────────────────────────────────────


_async_redis: Any = None
_async_redis_lock: asyncio.Lock | None = None

# Maximum webhook request body size (10 MB).
MAX_BODY_BYTES = 10 * 1024 * 1024


async def _get_async_redis() -> Any:
    """Get or create an async Redis connection with health check.

    Uses asyncio.Lock to prevent race conditions on concurrent init.
    Pings existing connections to detect stale/dead sockets.
    """
    global _async_redis, _async_redis_lock
    if _async_redis_lock is None:
        _async_redis_lock = asyncio.Lock()
    async with _async_redis_lock:
        # Health-check existing connection
        if _async_redis is not None:
            try:
                await _async_redis.ping()
                return _async_redis
            except Exception:
                logger.warning("Async Redis connection stale, reconnecting")
                _async_redis = None

        try:
            import redis.asyncio as aioredis

            from robothor.config import get_config

            cfg = get_config()
            r = aioredis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=cfg.redis.password or None,
                socket_connect_timeout=5,
            )
            await r.ping()  # type: ignore[misc]
            _async_redis = r
            return _async_redis
        except Exception as e:
            logger.warning("Failed to create async Redis connection: %s", e)
            return None


async def _check_rate_limit(channel: WebhookChannel) -> bool:
    """Check rate limit using Redis INCR + EXPIRE pattern.

    Returns True if the request is allowed, False if rate-limited.
    Fails CLOSED (rejects) when Redis is unavailable to prevent abuse.
    """
    try:
        r = await _get_async_redis()
        if r is None:
            logger.warning("Rate limit check: Redis unavailable, rejecting request")
            return False

        key = f"robothor:webhook:rate:{channel.name}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 60)
        return bool(count <= channel.rate_limit_per_min)
    except Exception as e:
        logger.warning("Rate limit check failed (rejecting): %s", e)
        return False


async def _publish_to_stream(stream: str, event_type: str, payload: dict[str, Any]) -> str:
    """Publish a webhook event to a Redis stream.

    Returns the event ID.
    """
    event_id = str(uuid.uuid4())
    envelope: dict[str, str] = {
        "id": event_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "type": event_type,
        "source": "webhook",
        "actor": "external",
        "payload": json.dumps(payload),
    }

    try:
        r = await _get_async_redis()
        if r is None:
            return event_id

        stream_key = f"{STREAM_PREFIX}{stream}"
        await r.xadd(stream_key, envelope, maxlen=10000)
        logger.info("Published webhook event %s to %s", event_id, stream_key)
    except Exception as e:
        logger.error("Failed to publish webhook event: %s", e)

    return event_id


def get_webhook_router(config: WebhookConfig | None = None) -> APIRouter:
    """Create a FastAPI APIRouter for webhook ingress.

    Args:
        config: Webhook configuration. If None, loads from default path.
    """
    if config is None:
        default_path = Path.home() / "robothor" / "docs" / "webhooks.yaml"
        config = load_webhook_config(default_path)

    router = APIRouter(tags=["webhooks"])

    @router.post("/api/webhooks/{channel}")
    async def receive_webhook(channel: str, request: Request) -> JSONResponse:
        """Receive a webhook POST and publish to Redis."""
        # Look up channel
        ch = config.channels.get(channel)
        if ch is None:
            return JSONResponse(
                {"error": f"Unknown channel: {channel}"},
                status_code=404,
            )

        if not ch.enabled:
            return JSONResponse(
                {"error": f"Channel disabled: {channel}"},
                status_code=404,
            )

        # Read body (enforce size limit)
        content_length = request.headers.get("content-length")
        try:
            cl = int(content_length) if content_length else 0
        except (ValueError, TypeError):
            cl = 0
        if cl > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": f"Payload too large (max {MAX_BODY_BYTES // 1024 // 1024}MB)"},
                status_code=413,
            )
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": f"Payload too large (max {MAX_BODY_BYTES // 1024 // 1024}MB)"},
                status_code=413,
            )

        # Verify HMAC signature
        secret = os.getenv(ch.secret_env, "")
        signature = (
            request.headers.get("X-Hub-Signature-256", "")
            or request.headers.get("X-Signature-256", "")
            or request.headers.get("X-Webhook-Signature", "")
        )

        if not secret:
            # Secret env var not set — reject to prevent unauthenticated ingress
            logger.warning(
                "Webhook channel rejected: secret not configured for channel '%s'",
                ch.name,
            )
            return JSONResponse(
                {"error": "Channel not configured: webhook secret not set"},
                status_code=503,
            )

        if not _verify_hmac(body, signature, secret):
            return JSONResponse(
                {"error": "Invalid signature"},
                status_code=401,
            )

        # Rate limiting
        if not await _check_rate_limit(ch):
            return JSONResponse(
                {"error": "Rate limit exceeded"},
                status_code=429,
            )

        # Parse payload (reject invalid JSON)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "Invalid JSON payload"},
                status_code=400,
            )

        # Extract event type
        event_type = ""
        if ch.event_type_header:
            event_type = request.headers.get(ch.event_type_header, "")
        elif ch.event_type_field and isinstance(payload, dict):
            event_type = str(payload.get(ch.event_type_field, ""))

        # Apply prefix
        if ch.event_type_prefix and event_type:
            event_type = f"{ch.event_type_prefix}{event_type}"
        elif ch.event_type_prefix:
            event_type = ch.event_type_prefix.rstrip(".")

        # Publish to Redis
        event_id = await _publish_to_stream(ch.stream, event_type, payload)

        # Record stats
        _record_received(channel)

        return JSONResponse(
            {"status": "accepted", "event_id": event_id, "event_type": event_type},
            status_code=200,
        )

    @router.get("/api/webhooks")
    async def list_channels() -> JSONResponse:
        """List configured webhook channels and their stats."""
        result = []
        for name, ch in (config.channels or {}).items():
            stats = _get_stats(name)
            result.append(
                {
                    "name": name,
                    "stream": ch.stream,
                    "enabled": ch.enabled,
                    "rate_limit_per_min": ch.rate_limit_per_min,
                    "total_received": stats["total_received"],
                    "last_received_at": stats["last_received_at"],
                }
            )
        return JSONResponse({"channels": result})

    return router
