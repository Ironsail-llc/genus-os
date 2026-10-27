"""Tests for webhook ingress — robothor/engine/webhooks.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import textwrap
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from robothor.engine.webhooks import (
    WebhookChannel,
    WebhookConfig,
    _channel_stats,
    _publish_to_stream,
    _verify_hmac,
    _verify_stripe_t_v1,
    get_webhook_router,
    load_webhook_config,
)

# ── HMAC verification tests ────────────────────────────────────────


def test_verify_hmac_valid():
    """Valid HMAC-SHA256 signature passes verification."""
    secret = "test-secret-key"
    payload = b'{"action":"push"}'
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert _verify_hmac(payload, sig, secret) is True


def test_verify_hmac_invalid():
    """Invalid HMAC-SHA256 signature fails verification."""
    secret = "test-secret-key"
    payload = b'{"action":"push"}'
    assert _verify_hmac(payload, "deadbeef" * 8, secret) is False


def test_verify_hmac_github_format():
    """GitHub-style sha256=XXXX format is accepted."""
    secret = "gh-webhook-secret"
    payload = b'{"ref":"refs/heads/main"}'
    raw_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    github_sig = f"sha256={raw_sig}"
    assert _verify_hmac(payload, github_sig, secret) is True


def test_verify_hmac_empty_signature():
    """Empty signature fails."""
    assert _verify_hmac(b"data", "", "secret") is False


def test_verify_hmac_empty_secret():
    """Empty secret fails."""
    assert _verify_hmac(b"data", "abcd", "") is False


# ── Config loading tests ───────────────────────────────────────────


def test_load_webhook_config(tmp_path: Path):
    """Loads YAML config and parses channels correctly."""
    config_file = tmp_path / "webhooks.yaml"
    config_file.write_text(
        textwrap.dedent("""\
        channels:
          github:
            stream: github
            secret_env: GITHUB_WEBHOOK_SECRET
            event_type_header: X-GitHub-Event
            event_type_prefix: "github."
            rate_limit_per_min: 60
          stripe:
            stream: payments
            secret_env: STRIPE_WEBHOOK_SECRET
            event_type_field: type
            rate_limit_per_min: 30
    """)
    )

    cfg = load_webhook_config(config_file)
    assert len(cfg.channels) == 2
    assert "github" in cfg.channels
    assert "stripe" in cfg.channels

    gh = cfg.channels["github"]
    assert gh.stream == "github"
    assert gh.secret_env == "GITHUB_WEBHOOK_SECRET"
    assert gh.event_type_header == "X-GitHub-Event"
    assert gh.event_type_prefix == "github."
    assert gh.rate_limit_per_min == 60

    stripe = cfg.channels["stripe"]
    assert stripe.stream == "payments"
    assert stripe.event_type_field == "type"
    assert stripe.rate_limit_per_min == 30


def test_load_webhook_config_missing_file(tmp_path: Path):
    """Missing config file returns empty config."""
    cfg = load_webhook_config(tmp_path / "nonexistent.yaml")
    assert len(cfg.channels) == 0


# ── FastAPI endpoint tests ─────────────────────────────────────────


@pytest.fixture()
def _clear_stats():
    """Clear channel stats between tests."""
    _channel_stats.clear()
    yield
    _channel_stats.clear()


@pytest.fixture()
def client(_clear_stats):
    """FastAPI TestClient with webhook routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    config = WebhookConfig(
        channels={
            "github": WebhookChannel(
                name="github",
                stream="github",
                secret_env="TEST_WEBHOOK_SECRET",
                event_type_header="X-GitHub-Event",
                event_type_prefix="github.",
                rate_limit_per_min=5,
            ),
            "generic": WebhookChannel(
                name="generic",
                stream="webhook",
                secret_env="TEST_GENERIC_SECRET",
                event_type_field="event_type",
                event_type_prefix="",
                rate_limit_per_min=100,
            ),
        }
    )

    app = FastAPI()
    app.include_router(get_webhook_router(config))
    return TestClient(app)


def _sign(payload: bytes, secret: str) -> str:
    """Generate sha256=XXXX signature."""
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


@patch.dict("os.environ", {"TEST_WEBHOOK_SECRET": "mysecret"})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch(
    "robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="evt-123"
)
def test_webhook_endpoint_valid_request(mock_publish, mock_rate, client):
    """Valid POST with correct HMAC returns 200."""
    payload = json.dumps({"action": "push", "ref": "refs/heads/main"}).encode()
    sig = _sign(payload, "mysecret")

    resp = client.post(
        "/api/webhooks/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["event_type"] == "github.push"
    assert data["event_id"] == "evt-123"
    mock_publish.assert_called_once()


@patch.dict("os.environ", {"TEST_WEBHOOK_SECRET": "mysecret"})
def test_webhook_endpoint_invalid_signature(client):
    """POST with wrong HMAC returns 401."""
    payload = json.dumps({"action": "push"}).encode()

    resp = client.post(
        "/api/webhooks/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=badhex",
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 401
    assert "Invalid signature" in resp.json()["error"]


def test_webhook_endpoint_unknown_channel(client):
    """POST to unknown channel returns 404."""
    resp = client.post(
        "/api/webhooks/nonexistent",
        content=b'{"x":1}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404


@patch.dict("os.environ", {"TEST_WEBHOOK_SECRET": "mysecret"})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=False)
def test_webhook_endpoint_rate_limited(mock_rate, client):
    """Returns 429 when rate limit is exceeded."""
    payload = json.dumps({"action": "push"}).encode()
    sig = _sign(payload, "mysecret")

    resp = client.post(
        "/api/webhooks/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 429
    assert "Rate limit" in resp.json()["error"]


def test_list_channels(client):
    """GET /api/webhooks returns configured channels."""
    resp = client.get("/api/webhooks")
    assert resp.status_code == 200
    data = resp.json()
    channels = data["channels"]
    assert len(channels) == 2
    names = {c["name"] for c in channels}
    assert names == {"github", "generic"}
    # Check fields are present
    gh = next(c for c in channels if c["name"] == "github")
    assert gh["stream"] == "github"
    assert gh["rate_limit_per_min"] == 5
    assert gh["total_received"] == 0


def test_webhook_endpoint_secret_not_configured(client):
    """POST when secret env var is not set returns 503."""
    # Don't patch TEST_WEBHOOK_SECRET into env
    resp = client.post(
        "/api/webhooks/github",
        content=b'{"test": true}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]


# ── Per-channel signature schemes ──────────────────────────────────

PLATFORM_SECRET = "platform-secret"


def _stripe_sig(payload: bytes, secret: str, ts: int) -> str:
    """Build a ``t=<ts>,v1=<hex>`` header value."""
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_load_webhook_config_signature_fields(tmp_path: Path):
    """New per-channel signature fields are parsed; defaults keep old behaviour."""
    config_file = tmp_path / "webhooks.yaml"
    config_file.write_text(
        textwrap.dedent("""\
        channels:
          platform:
            stream: platform
            secret_env: PLATFORM_WEBHOOK_SECRET
            signature_header: X-Platform-Signature
            signature_scheme: stripe_t_v1
            timestamp_tolerance_seconds: 120
            delivery_id_header: X-Platform-Delivery
          plain:
            stream: webhook
            secret_env: PLAIN_SECRET
    """)
    )
    cfg = load_webhook_config(config_file)
    p = cfg.channels["platform"]
    assert p.signature_header == "X-Platform-Signature"
    assert p.signature_scheme == "stripe_t_v1"
    assert p.timestamp_tolerance_seconds == 120
    assert p.delivery_id_header == "X-Platform-Delivery"

    plain = cfg.channels["plain"]
    assert plain.signature_header == ""
    assert plain.signature_scheme == "hmac_hex"
    assert plain.timestamp_tolerance_seconds == 300
    assert plain.delivery_id_header == ""


class TestVerifyStripeTV1:
    """Unit tests for the ``t=...,v1=...`` verifier."""

    body = b'{"type":"order.created"}'

    def test_valid(self):
        now = 1_700_000_000
        header = _stripe_sig(self.body, PLATFORM_SECRET, now)
        ok, _reason = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=now)
        assert ok is True

    def test_any_v1_may_match(self):
        """During secret rotation the sender emits several v1 values."""
        now = 1_700_000_000
        good = _stripe_sig(self.body, PLATFORM_SECRET, now).split(",")[1]
        header = f"t={now},v1={'0' * 64},{good}"
        ok, _ = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=now)
        assert ok is True

    def test_stale_timestamp(self):
        now = 1_700_000_000
        header = _stripe_sig(self.body, PLATFORM_SECRET, now - 301)
        ok, reason = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=now)
        assert ok is False
        assert "tolerance" in reason

    def test_future_timestamp(self):
        now = 1_700_000_000
        header = _stripe_sig(self.body, PLATFORM_SECRET, now + 301)
        ok, _ = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=now)
        assert ok is False

    def test_wrong_secret(self):
        now = 1_700_000_000
        header = _stripe_sig(self.body, "other-secret", now)
        ok, reason = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=now)
        assert ok is False
        assert PLATFORM_SECRET not in reason

    @pytest.mark.parametrize(
        "header",
        ["", "garbage", "t=abc,v1=deadbeef", "t=1700000000", "v1=deadbeef", "t=,v1="],
    )
    def test_malformed(self, header):
        ok, _ = _verify_stripe_t_v1(self.body, header, PLATFORM_SECRET, 300, now=1_700_000_000)
        assert ok is False


@pytest.fixture()
def platform_client(_clear_stats):
    """TestClient with a t/v1-signed channel that has delivery idempotency."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    config = WebhookConfig(
        channels={
            "platform": WebhookChannel(
                name="platform",
                stream="platform",
                secret_env="TEST_PLATFORM_SECRET",
                event_type_field="type",
                event_type_prefix="platform.",
                signature_header="X-Platform-Signature",
                signature_scheme="stripe_t_v1",
                timestamp_tolerance_seconds=300,
                delivery_id_header="X-Platform-Delivery",
            ),
            "custom-header": WebhookChannel(
                name="custom-header",
                stream="webhook",
                secret_env="TEST_PLATFORM_SECRET",
                event_type_field="type",
                signature_header="X-Custom-Sig",
            ),
        }
    )
    app = FastAPI()
    app.include_router(get_webhook_router(config))
    return TestClient(app)


def _dedup_redis(first_seen: bool = True) -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True if first_seen else None)
    return redis


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="evt-1")
def test_stripe_scheme_valid_request(mock_publish, mock_rate, platform_client):
    payload = json.dumps({"type": "order.created", "id": 7}).encode()
    redis = _dedup_redis(first_seen=True)
    with patch("robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock) as get_r:
        get_r.return_value = redis
        resp = platform_client.post(
            "/api/webhooks/platform",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Platform-Signature": _stripe_sig(payload, PLATFORM_SECRET, int(time.time())),
                "X-Platform-Delivery": "dlv-1",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    assert resp.json()["event_type"] == "platform.order.created"
    redis.set.assert_awaited_once_with(
        "robothor:webhook:delivery:platform:dlv-1", 1, nx=True, ex=86400
    )
    mock_publish.assert_awaited_once()
    assert mock_publish.call_args.kwargs["delivery_id"] == "dlv-1"


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "make_header",
    [
        lambda p: _stripe_sig(p, PLATFORM_SECRET, int(time.time()) - 3600),  # stale
        lambda p: _stripe_sig(p, "wrong-secret", int(time.time())),  # wrong secret
        lambda p: "t=notanumber,v1=abc",  # malformed
        lambda p: _sign(p, PLATFORM_SECRET),  # hmac_hex value on a t/v1 channel
    ],
    ids=["stale", "wrong-secret", "malformed", "wrong-scheme"],
)
def test_stripe_scheme_rejected(mock_publish, mock_rate, make_header, platform_client):
    payload = json.dumps({"type": "order.created"}).encode()
    resp = platform_client.post(
        "/api/webhooks/platform",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Platform-Signature": make_header(payload),
            "X-Platform-Delivery": "dlv-x",
        },
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "Invalid signature"}
    mock_publish.assert_not_called()


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_replayed_delivery_is_duplicate(mock_publish, mock_rate, platform_client):
    payload = json.dumps({"type": "order.created"}).encode()
    with patch("robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock) as get_r:
        get_r.return_value = _dedup_redis(first_seen=False)
        resp = platform_client.post(
            "/api/webhooks/platform",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Platform-Signature": _stripe_sig(payload, PLATFORM_SECRET, int(time.time())),
                "X-Platform-Delivery": "dlv-1",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate"}
    mock_publish.assert_not_called()


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_delivery_check_fails_closed_without_redis(mock_publish, mock_rate, platform_client):
    payload = json.dumps({"type": "order.created"}).encode()
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=None
    ):
        resp = platform_client.post(
            "/api/webhooks/platform",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Platform-Signature": _stripe_sig(payload, PLATFORM_SECRET, int(time.time())),
                "X-Platform-Delivery": "dlv-1",
            },
        )
    assert resp.status_code == 503
    mock_publish.assert_not_called()


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="")
def test_failed_publish_releases_delivery_claim(mock_publish, mock_rate, platform_client):
    """A claimed delivery whose publish fails is released so the sender's retry lands."""
    payload = json.dumps({"type": "order.created"}).encode()
    redis = _dedup_redis(first_seen=True)
    with patch("robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock) as get_r:
        get_r.return_value = redis
        resp = platform_client.post(
            "/api/webhooks/platform",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Platform-Signature": _stripe_sig(payload, PLATFORM_SECRET, int(time.time())),
                "X-Platform-Delivery": "dlv-1",
            },
        )
    assert resp.status_code == 503
    redis.delete.assert_awaited_once_with("robothor:webhook:delivery:platform:dlv-1")


@patch.dict("os.environ", {"TEST_PLATFORM_SECRET": PLATFORM_SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="evt-2")
def test_signature_header_is_the_only_header_read(mock_publish, mock_rate, platform_client):
    payload = json.dumps({"type": "x"}).encode()
    sig = _sign(payload, PLATFORM_SECRET)

    # The legacy headers are ignored once signature_header is set
    resp = platform_client.post(
        "/api/webhooks/custom-header",
        content=payload,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 401

    resp = platform_client.post(
        "/api/webhooks/custom-header",
        content=payload,
        headers={"Content-Type": "application/json", "X-Custom-Sig": sig},
    )
    assert resp.status_code == 200
    # No delivery_id_header on this channel: publish carries no delivery id
    assert mock_publish.call_args.kwargs.get("delivery_id", "") == ""


@patch.dict("os.environ", {"TEST_WEBHOOK_SECRET": "mysecret"})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="evt-3")
@pytest.mark.parametrize(
    "header", ["X-Hub-Signature-256", "X-Signature-256", "X-Webhook-Signature"]
)
def test_hmac_hex_default_headers_unchanged(mock_publish, mock_rate, header, client):
    payload = json.dumps({"action": "push"}).encode()
    resp = client.post(
        "/api/webhooks/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            header: _sign(payload, "mysecret"),
            "X-GitHub-Event": "push",
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_publish_envelope_carries_delivery_and_event_type():
    redis = AsyncMock()
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=redis
    ):
        event_id = await _publish_to_stream(
            "platform", "platform.order.created", {"id": 1}, delivery_id="dlv-9"
        )
    assert event_id
    key, envelope = redis.xadd.call_args.args[:2]
    assert key == "robothor:events:platform"
    assert envelope["type"] == "platform.order.created"
    assert envelope["event_type"] == "platform.order.created"
    assert envelope["delivery_id"] == "dlv-9"
    assert json.loads(envelope["payload"]) == {"id": 1}


@pytest.mark.asyncio
async def test_publish_envelope_omits_unknown_delivery_id():
    redis = AsyncMock()
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=redis
    ):
        await _publish_to_stream("webhook", "x", {"id": 1})
    envelope = redis.xadd.call_args.args[1]
    assert "delivery_id" not in envelope


@patch.dict("os.environ", {"TEST_WEBHOOK_SECRET": "mysecret"})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="")
def test_failed_publish_is_503_and_not_counted(mock_publish, mock_rate, client):
    """Without a delivery id too: the sender is asked to retry, stats are untouched."""
    payload = json.dumps({"action": "push"}).encode()
    resp = client.post(
        "/api/webhooks/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(payload, "mysecret"),
            "X-GitHub-Event": "push",
        },
    )
    assert resp.status_code == 503
    assert "github" not in _channel_stats
