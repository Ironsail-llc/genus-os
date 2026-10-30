"""The ``hmac_ts_hex`` webhook scheme: hex HMAC over ``"<timestamp>.<body>"``."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import textwrap
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.webhooks import (
    SIGNATURE_SCHEMES,
    _channel_stats,
    _verify_hmac_ts_hex,
    get_webhook_router,
    load_webhook_config,
)

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "ts-hex-secret"
NOW = 1_700_000_000
BODY = b'{"event":"ticket.created","id":1}'

YAML = textwrap.dedent("""\
    channels:
      desk:
        stream: desk
        secret_env: TEST_TS_HEX_SECRET
        signature_scheme: hmac_ts_hex
        signature_header: X-Desk-Signature
        timestamp_header: X-Desk-Timestamp
        event_type_field: event
        event_type_prefix: "desk."
      desk-default-ts:
        stream: desk
        secret_env: TEST_TS_HEX_SECRET
        signature_scheme: hmac_ts_hex
        signature_header: X-Desk-Signature
""")


def _digest(body: bytes, secret: str, ts: int | str) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


# ── config ─────────────────────────────────────────────────────────


def test_scheme_is_known():
    assert "hmac_ts_hex" in SIGNATURE_SCHEMES


def test_config_loads_timestamp_header(tmp_path: Path, caplog):
    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    with caplog.at_level(logging.WARNING, logger="robothor.engine.webhooks"):
        cfg = load_webhook_config(path)
    assert cfg.channels["desk"].signature_scheme == "hmac_ts_hex"
    assert cfg.channels["desk"].timestamp_header == "X-Desk-Timestamp"
    assert cfg.channels["desk-default-ts"].timestamp_header == "X-Webhook-Timestamp"
    assert "unknown signature_scheme" not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"), [("  X-Desk-Ts  ", "X-Desk-Ts"), ("   ", "X-Webhook-Timestamp")]
)
def test_config_strips_timestamp_header(tmp_path: Path, raw, expected):
    path = tmp_path / "webhooks.yaml"
    path.write_text(
        f'channels:\n  desk:\n    signature_scheme: hmac_ts_hex\n    timestamp_header: "{raw}"\n'
    )
    assert load_webhook_config(path).channels["desk"].timestamp_header == expected


# ── verifier ───────────────────────────────────────────────────────


class TestVerifyHmacTsHex:
    def _verify(self, signature: str, timestamp: str, secret: str = SECRET, now: float = NOW):
        return _verify_hmac_ts_hex(BODY, signature, timestamp, secret, 300, now=now)

    def test_bare_hex_digest(self):
        ok, reason = self._verify(_digest(BODY, SECRET, NOW), str(NOW))
        assert ok is True, reason

    @pytest.mark.parametrize("prefix", ["sha256=", "SHA256=", "Sha256="])
    def test_prefixed_digest(self, prefix):
        ok, reason = self._verify(prefix + _digest(BODY, SECRET, NOW), str(NOW))
        assert ok is True, reason

    def test_missing_timestamp(self):
        ok, reason = self._verify(_digest(BODY, SECRET, NOW), "")
        assert ok is False
        assert "missing timestamp" in reason

    @pytest.mark.parametrize(
        "ts",
        [
            "abc",
            "1700000000.5",
            "-1700000000",
            "+1700000000",
            " 1700000000",
            "1" + "0" * 400,
            "١٧",
        ],
    )
    def test_malformed_timestamp(self, ts):
        ok, reason = self._verify(_digest(BODY, SECRET, ts), ts)
        assert ok is False
        assert reason == "malformed timestamp"

    def test_milliseconds_are_not_seconds(self):
        """Seconds only: a millisecond timestamp reads as far in the future."""
        ms = NOW * 1000
        ok, reason = self._verify(_digest(BODY, SECRET, ms), str(ms))
        assert ok is False
        assert "stale timestamp" in reason

    @pytest.mark.parametrize("offset", [-301, 301])
    def test_stale_timestamp(self, offset):
        ts = NOW + offset
        ok, reason = self._verify(_digest(BODY, SECRET, ts), str(ts))
        assert ok is False
        assert "stale timestamp" in reason

    def test_within_tolerance(self):
        ts = NOW - 299
        ok, reason = self._verify(_digest(BODY, SECRET, ts), str(ts))
        assert ok is True, reason

    def test_bad_digest_never_names_secret(self):
        ok, reason = self._verify(_digest(BODY, "other-secret", NOW), str(NOW))
        assert ok is False
        assert "bad digest" in reason
        assert SECRET not in reason

    def test_digest_bound_to_timestamp(self):
        """A digest signed for one timestamp fails under another."""
        ok, reason = self._verify(_digest(BODY, SECRET, NOW - 10), str(NOW))
        assert ok is False
        assert "bad digest" in reason

    @pytest.mark.parametrize("sig", ["", "sha256=", "SHA256="])
    def test_missing_signature(self, sig):
        ok, reason = self._verify(sig, str(NOW))
        assert ok is False
        assert reason == "missing signature"

    @pytest.mark.parametrize(
        "sig",
        [
            "\xe9" * 64,  # non-ASCII
            "g" * 64,  # not hex
            "a" * 63,  # too short
            "a" * 65,  # too long
            "sha256=" + "a" * 40,  # a SHA-1-sized digest
            " " + "a" * 64,
        ],
    )
    def test_malformed_signature(self, sig):
        ok, reason = self._verify(sig, str(NOW))
        assert ok is False
        assert reason == "malformed signature"

    def test_missing_secret(self):
        ok, reason = self._verify(_digest(BODY, SECRET, NOW), str(NOW), secret="")
        assert ok is False
        assert reason == "missing secret"


# ── endpoint ───────────────────────────────────────────────────────


@pytest.fixture()
def ts_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _channel_stats.clear()
    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    app = FastAPI()
    app.include_router(get_webhook_router(load_webhook_config(path)))
    yield TestClient(app)
    _channel_stats.clear()


@patch.dict("os.environ", {"TEST_TS_HEX_SECRET": SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="e-1")
def test_endpoint_accepts_signed_request(mock_publish, mock_rate, ts_client):
    payload = json.dumps({"event": "ticket.created"}).encode()
    ts = int(time.time())
    resp = ts_client.post(
        "/api/webhooks/desk",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Desk-Signature": "sha256=" + _digest(payload, SECRET, ts),
            "X-Desk-Timestamp": str(ts),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_type"] == "desk.ticket.created"
    mock_publish.assert_awaited_once()


@patch.dict("os.environ", {"TEST_TS_HEX_SECRET": SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="e-1")
def test_endpoint_reads_default_timestamp_header(mock_publish, mock_rate, ts_client):
    payload = json.dumps({"event": "ticket.created"}).encode()
    ts = int(time.time())
    resp = ts_client.post(
        "/api/webhooks/desk-default-ts",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Desk-Signature": _digest(payload, SECRET, ts),
            "X-Webhook-Timestamp": str(ts),
        },
    )
    assert resp.status_code == 200, resp.text


@patch.dict("os.environ", {"TEST_TS_HEX_SECRET": SECRET})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
@pytest.mark.parametrize(
    ("make_headers", "reason"),
    [
        (lambda p, ts: {"X-Desk-Signature": _digest(p, SECRET, ts)}, "missing timestamp"),
        (
            lambda p, ts: {
                "X-Desk-Signature": _digest(p, SECRET, ts - 3600),
                "X-Desk-Timestamp": str(ts - 3600),
            },
            "stale timestamp",
        ),
        (
            lambda p, ts: {
                "X-Desk-Signature": _digest(p, "wrong-secret", ts),
                "X-Desk-Timestamp": str(ts),
            },
            "bad digest",
        ),
        (
            # the timestamp in the wrong header is not read
            lambda p, ts: {
                "X-Desk-Signature": _digest(p, SECRET, ts),
                "X-Webhook-Timestamp": str(ts),
            },
            "missing timestamp",
        ),
        (lambda p, ts: {"X-Desk-Timestamp": str(ts)}, "missing signature"),
    ],
    ids=["no-timestamp", "stale", "wrong-secret", "wrong-timestamp-header", "no-signature"],
)
def test_endpoint_refuses(mock_publish, mock_rate, make_headers, reason, ts_client, caplog):
    payload = json.dumps({"event": "ticket.created"}).encode()
    headers = {"Content-Type": "application/json", **make_headers(payload, int(time.time()))}
    with caplog.at_level(logging.WARNING, logger="robothor.engine.webhooks"):
        resp = ts_client.post("/api/webhooks/desk", content=payload, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "Invalid signature"}
    assert reason in caplog.text
    assert SECRET not in caplog.text
    mock_publish.assert_not_called()
