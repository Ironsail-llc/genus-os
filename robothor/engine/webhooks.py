"""Webhook ingress — receive external events and publish to Redis event bus.

External services POST to /api/webhooks/{channel} with an HMAC-SHA256 signature.
Each channel picks its signature scheme: ``hmac_hex`` (hex digest of the raw
body, GitHub-style), ``stripe_t_v1`` (``t=<unix>,v1=<hex>`` over
``"<t>." + body`` with a timestamp window) or ``hmac_ts_hex`` (hex digest of
``"<timestamp>." + body`` in the signature header, optionally ``sha256=``
prefixed, with the integer unix-seconds timestamp in the channel's
``timestamp_header`` and the same timestamp window). A channel may also name a
delivery id header, in which case a replayed delivery is answered as a duplicate and
not re-published. A ``google_oidc`` channel takes Google Cloud Pub/Sub push
instead: the ``Authorization: Bearer`` ID token is verified against the
channel's audience and service account, the Pub/Sub ``messageId`` is the
delivery id, and the base64 ``message.data`` is decoded into the payload. The
payload is normalized and published to the appropriate Redis stream.
Agents pick up events via the existing EventHooks system.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Must match hooks.py / event_bus.py
STREAM_PREFIX = "robothor:events:"

SIGNATURE_SCHEMES = ("hmac_hex", "stripe_t_v1", "hmac_ts_hex", "google_oidc")

# Headers tried in order when a channel names no signature_header.
DEFAULT_SIGNATURE_HEADERS = ("X-Hub-Signature-256", "X-Signature-256", "X-Webhook-Signature")

# hmac_ts_hex: header carrying the signed timestamp when a channel names none.
DEFAULT_TIMESTAMP_HEADER = "X-Webhook-Timestamp"

# How long a delivery id is remembered for replay detection.
DELIVERY_ID_TTL_SECONDS = 86400


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
    # When set, the signature is read from this header only.
    signature_header: str = ""
    # One of SIGNATURE_SCHEMES.
    signature_scheme: str = "hmac_hex"
    # stripe_t_v1 and hmac_ts_hex: how far the signed timestamp may drift from now.
    timestamp_tolerance_seconds: int = 300
    # hmac_ts_hex only: header carrying the signed timestamp (unix seconds).
    timestamp_header: str = DEFAULT_TIMESTAMP_HEADER
    # When set and present on a request, replays of the same id are dropped.
    delivery_id_header: str = ""
    # google_oidc only: the token's required audience and signing service account.
    oidc_audience: str = ""
    oidc_service_account: str = ""


@dataclass
class WebhookConfig:
    """Top-level webhook configuration."""

    channels: dict[str, WebhookChannel] = field(default_factory=dict)


def _dropin_dir(path: Path) -> Path:
    """``docs/webhooks.yaml`` -> ``docs/webhooks.d``."""
    return path.with_name(f"{path.stem}.d")


def _read_channels(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("channels") if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def load_webhook_config(path: str | Path) -> WebhookConfig:
    """Load webhook channel definitions from a YAML file and its drop-ins.

    Every ``*.yaml`` in the sibling ``<stem>.d/`` directory (``docs/webhooks.d/``
    beside ``docs/webhooks.yaml``) is merged after the base file, in name
    order, so a plugin can own a file of its own channels. A channel declared
    twice -- in the base file and a drop-in, or in two drop-ins -- is a
    ``ValueError`` naming both files, and a file that does not parse raises. A
    missing directory is fine.
    """
    return _load_webhook_config(Path(path), strict=True)


def _load_webhook_config(path: Path, *, strict: bool) -> WebhookConfig:
    """:func:`load_webhook_config`; with ``strict=False`` a DROP-IN that does not
    parse, a channel a drop-in declares again, or a malformed drop-in channel is
    logged and skipped, so the router still serves every channel it can. The
    base file is the operator's and stays strict either way."""
    dropins = _dropin_dir(path)
    sources = sorted(dropins.glob("*.yaml")) if dropins.is_dir() else []
    if path.exists():
        sources.insert(0, path)
    elif not sources:
        logger.warning("Webhook config not found: %s", path)
        return WebhookConfig()

    declared: dict[str, Path] = {}
    merged: dict[str, Any] = {}
    for source in sources:
        try:
            found = _read_channels(source)
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            if strict or source == path:
                raise
            logger.error(
                "Webhook config %s is not readable (%s); none of its channels are served",
                source,
                type(exc).__name__,
            )
            continue
        for name, raw in found.items():
            if name in declared:
                message = (
                    f"Webhook channel {name!r} is declared in both {declared[name]} and "
                    f"{source}; rename one of them."
                )
                if strict:
                    raise ValueError(message)
                logger.error("%s Serving the one in %s.", message, declared[name])
                continue
            declared[name] = source
            merged[name] = raw

    channels: dict[str, WebhookChannel] = {}
    for name, raw in merged.items():
        if not isinstance(raw, dict):
            continue
        try:
            channels[name] = _channel(name, raw)
        except (ValueError, TypeError) as exc:
            if strict or declared[name] == path:
                raise
            logger.error(
                "Webhook channel %r in %s is malformed (%s); it is not served",
                name,
                declared[name],
                type(exc).__name__,
            )
            continue
        if channels[name].signature_scheme not in SIGNATURE_SCHEMES:
            logger.warning(
                "Webhook channel '%s' has unknown signature_scheme %r; every request will be "
                "rejected",
                name,
                channels[name].signature_scheme,
            )

    return WebhookConfig(channels=channels)


def _channel(name: str, raw: dict[str, Any]) -> WebhookChannel:
    return WebhookChannel(
        name=name,
        stream=raw.get("stream", name),
        secret_env=raw.get("secret_env", ""),
        event_type_header=raw.get("event_type_header", ""),
        event_type_field=raw.get("event_type_field", ""),
        event_type_prefix=raw.get("event_type_prefix", ""),
        enabled=raw.get("enabled", True),
        rate_limit_per_min=int(raw.get("rate_limit_per_min", 60)),
        signature_header=raw.get("signature_header", "") or "",
        signature_scheme=raw.get("signature_scheme", "hmac_hex") or "hmac_hex",
        timestamp_tolerance_seconds=int(raw.get("timestamp_tolerance_seconds", 300)),
        timestamp_header=str(raw.get("timestamp_header", "") or "").strip()
        or DEFAULT_TIMESTAMP_HEADER,
        delivery_id_header=raw.get("delivery_id_header", "") or "",
        oidc_audience=str(raw.get("oidc_audience", "") or ""),
        oidc_service_account=str(raw.get("oidc_service_account", "") or ""),
    )


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

    if not signature.isascii():
        return False
    return hmac.compare_digest(expected, signature)


def _verify_stripe_t_v1(
    payload_bytes: bytes,
    header: str,
    secret: str,
    tolerance_seconds: int,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify a ``t=<unix seconds>,v1=<hex>[,v1=<hex>...]`` signature.

    The signed content is ``f"{t}."`` followed by the raw body. Valid when the
    timestamp is within ``tolerance_seconds`` of now and any ``v1`` matches.
    Returns ``(ok, reason)``; the reason never contains the secret.
    """
    if not header or not secret:
        return False, "missing signature or secret"

    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            # Unix seconds; bounded so the float comparison below cannot overflow
            if not (value.isascii() and value.isdigit() and len(value) <= 12):
                return False, "malformed timestamp"
            timestamp = int(value)
        elif key == "v1" and value and value.isascii():
            candidates.append(value)

    if timestamp is None or not candidates:
        return False, "malformed signature header"

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False, f"timestamp outside tolerance ({tolerance_seconds}s)"

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if any(hmac.compare_digest(expected, c) for c in candidates):
        return True, ""
    return False, "no v1 signature matched"


_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _verify_hmac_ts_hex(
    payload_bytes: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    tolerance_seconds: int,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify a hex HMAC-SHA256 over ``f"{timestamp}."`` followed by the raw body.

    ``timestamp`` is the value of the channel's timestamp header, in integer
    unix seconds (milliseconds are not accepted: they read as far in the
    future). The signature may carry a ``sha256=`` prefix in any case. Returns
    ``(ok, reason)``; the reason names the failed check, never the secret.
    """
    if not secret:
        return False, "missing secret"
    if not timestamp:
        return False, "missing timestamp"
    # Bounded so the float comparison cannot overflow; 13 digits lets a
    # millisecond value through to be refused as stale rather than malformed.
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 13):
        return False, "malformed timestamp"
    ts = int(timestamp)
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        return False, f"stale timestamp (outside {tolerance_seconds}s)"

    if signature[:7].lower() == "sha256=":
        signature = signature[7:]
    if not signature:
        return False, "missing signature"
    if not _HEX_SHA256.fullmatch(signature):
        return False, "malformed signature"
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode() + payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(expected, signature.lower()):
        return True, ""
    return False, "bad digest"


def _verify_signature(
    channel: WebhookChannel,
    payload_bytes: bytes,
    signature: str,
    secret: str,
    timestamp: str = "",
) -> tuple[bool, str]:
    """Dispatch to the channel's signature scheme. Returns ``(ok, reason)``."""
    if channel.signature_scheme == "hmac_hex":
        if _verify_hmac(payload_bytes, signature, secret):
            return True, ""
        return False, "missing signature" if not signature else "hmac mismatch"
    if channel.signature_scheme == "stripe_t_v1":
        return _verify_stripe_t_v1(
            payload_bytes, signature, secret, channel.timestamp_tolerance_seconds
        )
    if channel.signature_scheme == "hmac_ts_hex":
        return _verify_hmac_ts_hex(
            payload_bytes, signature, timestamp, secret, channel.timestamp_tolerance_seconds
        )
    return False, f"unknown signature scheme {channel.signature_scheme!r}"


#: google_oidc verification runs on its own small pool, never the default
#: executor the rest of the engine's ``to_thread`` work shares.
OIDC_MAX_WORKERS = 4
OIDC_THREAD_PREFIX = "webhook-oidc"
#: Timeout for fetching Google's signing certificates.
OIDC_CERT_TIMEOUT_SECONDS = 5
#: How long fetched certificates are reused. Google publishes a new key well
#: before it signs with it, so a cached set stays valid across a rotation.
OIDC_CERT_TTL_SECONDS = 1800
#: Longest bearer token considered at all (a Google ID token is ~1 KB).
MAX_OIDC_TOKEN_CHARS = 8192

_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


def _looks_like_jwt(token: str) -> bool:
    """Three non-empty base64url segments, bounded in size: cheap, before any thread."""
    if not token or len(token) > MAX_OIDC_TOKEN_CHARS:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(_JWT_SEGMENT.fullmatch(p) for p in parts)


class CachedCertsRequest:
    """A google-auth transport that reuses a successful GET for ``ttl_seconds``.

    google-auth fetches the signing certificates on every verification; this
    answers a repeat GET of the same URL from memory and gives every real
    fetch a short timeout. Failed fetches are not cached.
    """

    def __init__(
        self,
        inner: Any,
        *,
        ttl_seconds: float = OIDC_CERT_TTL_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def __call__(self, url: str, method: str = "GET", **kwargs: Any) -> Any:
        if method.upper() != "GET":
            kwargs["timeout"] = OIDC_CERT_TIMEOUT_SECONDS
            return self._inner(url, method=method, **kwargs)
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None and self._clock() < hit[0]:
                return hit[1]
        kwargs["timeout"] = OIDC_CERT_TIMEOUT_SECONDS
        response = self._inner(url, method=method, **kwargs)
        if getattr(response, "status", None) == 200:
            with self._lock:
                self._cache[url] = (self._clock() + self._ttl, response)
        return response


_google_request: CachedCertsRequest | None = None
_oidc_pool: ThreadPoolExecutor | None = None


def _google_transport() -> CachedCertsRequest:
    """One shared, certificate-caching google-auth transport."""
    global _google_request
    if _google_request is None:
        from google.auth.transport.requests import Request

        _google_request = CachedCertsRequest(Request())
    return _google_request


def _oidc_executor() -> ThreadPoolExecutor:
    global _oidc_pool
    if _oidc_pool is None:
        _oidc_pool = ThreadPoolExecutor(
            max_workers=OIDC_MAX_WORKERS, thread_name_prefix=OIDC_THREAD_PREFIX
        )
    return _oidc_pool


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _check_google_oidc(channel: WebhookChannel, authorization: str) -> tuple[bool, str]:
    """Verify a Pub/Sub push ``Authorization: Bearer <ID token>``. Blocking (cert fetch).

    Valid when google-auth accepts the token for the channel's audience and
    its ``email`` claim is the channel's service account, verified.
    """
    token = _bearer_token(authorization)
    if not _looks_like_jwt(token):
        return False, "missing or malformed bearer token"
    try:
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, _google_transport(), audience=channel.oidc_audience
        )
    except Exception as exc:  # noqa: BLE001 - any verifier failure is a rejection
        return False, f"token rejected ({type(exc).__name__})"
    if not isinstance(claims, dict):
        return False, "token carried no claims"
    email = str(claims.get("email") or "")
    if email.lower() != channel.oidc_service_account.lower():
        return False, "token is not from the configured service account"
    if claims.get("email_verified") is not True:
        return False, "token email is not verified"
    return True, ""


def _unwrap_pubsub(body: Any) -> tuple[dict[str, Any], str] | None:
    """A Pub/Sub push envelope -> ``(payload, messageId)``; ``None`` when malformed.

    The payload is ``message.data`` decoded (a JSON object's keys, or
    ``{"data": <text>}``) plus ``pubsub``: the message id, publish time,
    attributes and subscription.
    """
    if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
        return None
    message = body["message"]
    message_id = str(message.get("messageId") or message.get("message_id") or "")
    try:
        raw = base64.b64decode(str(message.get("data") or ""), validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    try:
        decoded = json.loads(text) if text else {}
    except ValueError:
        decoded = None
    payload: dict[str, Any] = dict(decoded) if isinstance(decoded, dict) else {"data": text}
    payload["pubsub"] = {
        "message_id": message_id,
        "publish_time": str(message.get("publishTime") or message.get("publish_time") or ""),
        "attributes": message.get("attributes") or {},
        "subscription": str(body.get("subscription") or ""),
    }
    return payload, message_id


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
            await r.ping()  # type: ignore[misc,unused-ignore]
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


def _delivery_key(channel: WebhookChannel, delivery_id: str) -> str:
    return f"robothor:webhook:delivery:{channel.name}:{delivery_id}"


async def _claim_delivery(channel: WebhookChannel, delivery_id: str) -> bool | None:
    """Record a delivery id; ``True`` when first seen, ``False`` for a replay.

    Returns ``None`` when Redis is unavailable — the caller fails closed, as
    the rate limiter does.
    """
    try:
        r = await _get_async_redis()
        if r is None:
            logger.warning("Delivery check: Redis unavailable, rejecting request")
            return None
        claimed = await r.set(
            _delivery_key(channel, delivery_id), 1, nx=True, ex=DELIVERY_ID_TTL_SECONDS
        )
        return bool(claimed)
    except Exception as e:
        logger.warning("Delivery check failed (rejecting): %s", e)
        return None


async def _release_delivery(channel: WebhookChannel, delivery_id: str) -> None:
    """Forget a claimed delivery id so the sender's retry is accepted."""
    try:
        r = await _get_async_redis()
        if r is not None:
            await r.delete(_delivery_key(channel, delivery_id))
    except Exception as e:
        logger.warning("Failed to release delivery id: %s", e)


async def _publish_to_stream(
    stream: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str = "",
) -> str:
    """Publish a webhook event to a Redis stream.

    Returns the event ID, or ``""`` when the event could not be published.
    """
    event_id = str(uuid.uuid4())
    envelope: dict[str, str] = {
        "id": event_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "type": event_type,
        "source": "webhook",
        "actor": "external",
        "event_type": event_type,
        "payload": json.dumps(payload),
    }
    if delivery_id:
        envelope["delivery_id"] = delivery_id

    try:
        r = await _get_async_redis()
        if r is None:
            logger.error("Failed to publish webhook event: Redis unavailable")
            return ""

        stream_key = f"{STREAM_PREFIX}{stream}"
        await r.xadd(stream_key, envelope, maxlen=10000)
        logger.info("Published webhook event %s to %s", event_id, stream_key)
    except Exception as e:
        logger.error("Failed to publish webhook event: %s", e)
        return ""

    return event_id


async def _authenticate_google_oidc(ch: WebhookChannel, request: Request) -> JSONResponse | None:
    """Authenticate a Pub/Sub push; ``None`` when accepted, else the refusal.

    Cheap refusals come first (configuration, rate limit, token shape): the
    verifier costs a thread on :func:`_oidc_executor` and may fetch certs.
    """
    if not (ch.oidc_audience and ch.oidc_service_account):
        logger.warning(
            "Webhook channel rejected: oidc_audience/oidc_service_account not "
            "configured for channel '%s'",
            ch.name,
        )
        return JSONResponse(
            {"error": "Channel not configured: OIDC audience or account not set"},
            status_code=503,
        )
    if not await _check_rate_limit(ch):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
    authorization = request.headers.get("authorization", "")
    if not _looks_like_jwt(_bearer_token(authorization)):
        logger.warning("Webhook token rejected for channel '%s': malformed", ch.name)
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    ok, reason = await asyncio.get_running_loop().run_in_executor(
        _oidc_executor(), _check_google_oidc, ch, authorization
    )
    if not ok:
        logger.warning("Webhook token rejected for channel '%s': %s", ch.name, reason)
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    return None


def get_webhook_router(config: WebhookConfig | None = None) -> APIRouter:
    """Create a FastAPI APIRouter for webhook ingress.

    Args:
        config: Webhook configuration. If None, loads from default path.
    """
    if config is None:
        default_path = (
            Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
            / "docs"
            / "webhooks.yaml"
        )
        # Lenient: one bad drop-in must not take the engine's HTTP app down.
        config = _load_webhook_config(default_path, strict=False)

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

        oidc = ch.signature_scheme == "google_oidc"
        if oidc:
            refused = await _authenticate_google_oidc(ch, request)
            if refused is not None:
                return refused

        # Verify HMAC signature
        secret = "" if oidc else os.getenv(ch.secret_env, "")
        if oidc:
            signature = ""
        elif ch.signature_header:
            signature = request.headers.get(ch.signature_header, "")
        else:
            signature = next(
                (v for h in DEFAULT_SIGNATURE_HEADERS if (v := request.headers.get(h, ""))),
                "",
            )

        if not oidc and not secret:
            # Secret env var not set — reject to prevent unauthenticated ingress
            logger.warning(
                "Webhook channel rejected: secret not configured for channel '%s'",
                ch.name,
            )
            return JSONResponse(
                {"error": "Channel not configured: webhook secret not set"},
                status_code=503,
            )

        timestamp = (
            request.headers.get(ch.timestamp_header, "")
            if ch.signature_scheme == "hmac_ts_hex"
            else ""
        )
        ok, reason = (
            (True, "") if oidc else _verify_signature(ch, body, signature, secret, timestamp)
        )
        if not ok:
            logger.warning("Webhook signature rejected for channel '%s': %s", ch.name, reason)
            return JSONResponse(
                {"error": "Invalid signature"},
                status_code=401,
            )

        # Rate limiting (google_oidc channels were limited before verifying)
        if not oidc and not await _check_rate_limit(ch):
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

        pubsub_message_id = ""
        if oidc:
            unwrapped = _unwrap_pubsub(payload)
            if unwrapped is None:
                return JSONResponse(
                    {"error": "Invalid Pub/Sub push envelope"},
                    status_code=400,
                )
            payload, pubsub_message_id = unwrapped

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

        # Delivery idempotency: a replayed delivery id is acknowledged, not re-published
        delivery_id = ""
        if ch.delivery_id_header:
            delivery_id = request.headers.get(ch.delivery_id_header, "").strip()
        if not delivery_id and pubsub_message_id:
            delivery_id = pubsub_message_id
        if delivery_id:
            claimed = await _claim_delivery(ch, delivery_id)
            if claimed is None:
                return JSONResponse(
                    {"error": "Delivery check unavailable"},
                    status_code=503,
                )
            if not claimed:
                logger.info("Duplicate webhook delivery on channel '%s'", ch.name)
                return JSONResponse({"status": "duplicate"}, status_code=200)

        # Publish to Redis
        event_id = await _publish_to_stream(ch.stream, event_type, payload, delivery_id=delivery_id)
        if not event_id:
            # Ask the sender to retry; a claimed delivery id is released so the
            # retry is not answered as a duplicate
            if delivery_id:
                await _release_delivery(ch, delivery_id)
            return JSONResponse(
                {"error": "Event could not be published"},
                status_code=503,
            )

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
