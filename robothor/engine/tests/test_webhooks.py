"""Tests for webhook ingress — robothor/engine/webhooks.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from robothor.engine.webhooks import (
    WebhookChannel,
    WebhookConfig,
    _channel_stats,
    _verify_hmac,
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
