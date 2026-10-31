"""The standalone webhook receiver: ``python -m robothor.engine.hooks_server``.

It serves the engine's own webhook handler and nothing else, so it can run
with several replicas and keep accepting deliveries while the engine restarts.
Pinned here: its routes are exactly the webhook POST and two probes, readiness
follows Redis, a signed delivery lands on the stream the engine consumes with
the engine's dedupe key, each delivery logs one line without its payload, and
the entrypoint serves on ROBOTHOR_HOOKS_PORT.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import textwrap
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import hooks_server
from robothor.engine.hooks import STREAM_PREFIX as ENGINE_STREAM_PREFIX
from robothor.engine.webhooks import WebhookChannel, WebhookConfig, _channel_stats

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "hooks-test-secret"
PAYLOAD_MARKER = "do-not-log-this-payload-value"


class _FakeRedis:
    """The handful of async Redis calls the webhook handler makes."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}

    async def ping(self) -> bool:
        return True

    async def incr(self, key: str) -> int:
        self.values[key] = int(self.values.get(key, 0)) + 1
        return int(self.values[key])

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    async def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None) -> str:
        self.streams.setdefault(key, []).append(fields)
        return f"{len(self.streams[key])}-0"


@pytest.fixture(autouse=True)
def _clear_stats():
    _channel_stats.clear()
    yield
    _channel_stats.clear()


def _config() -> WebhookConfig:
    return WebhookConfig(
        channels={
            "orders": WebhookChannel(
                name="orders",
                stream="orders-events",
                secret_env="TEST_HOOKS_SECRET",
                event_type_field="event_type",
                delivery_id_header="X-Delivery-Id",
                rate_limit_per_min=100,
            )
        }
    )


def _client(config: WebhookConfig | None = None):
    from fastapi.testclient import TestClient

    return TestClient(hooks_server.create_app(config or _config()))


def _signed_post(client, body: bytes, delivery_id: str = "dlv-1"):
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/orders",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Delivery-Id": delivery_id,
        },
    )


# ── routes ─────────────────────────────────────────────────────────


def test_routes_are_exactly_the_webhook_post_and_the_two_probes():
    app = hooks_server.create_app(_config())

    # FastAPI keeps an included router as a lazy group rather than flattening
    # its routes into ``app.routes``, so walk into it.
    routes = [
        candidate
        for outer in app.routes
        for candidate in getattr(getattr(outer, "original_router", None), "routes", (outer,))
    ]
    served = {
        f"{method} {route.path}"
        for route in routes
        for method in (getattr(route, "methods", None) or {"WS"})
        if method != "HEAD"
    }

    assert served == {"POST /api/webhooks/{channel}", "GET /live", "GET /ready"}


def test_live_is_200_without_redis():
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=None
    ):
        resp = _client().get("/live")
    assert resp.status_code == 200


def test_ready_is_503_when_redis_is_unreachable():
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=None
    ):
        resp = _client().get("/ready")
    assert resp.status_code == 503


def test_ready_is_200_when_redis_pings():
    with patch(
        "robothor.engine.webhooks._get_async_redis",
        new_callable=AsyncMock,
        return_value=_FakeRedis(),
    ):
        resp = _client().get("/ready")
    assert resp.status_code == 200


def test_the_app_carries_no_engine_auth():
    """The handler authenticates the raw body itself; there is no bearer check."""
    redis = _FakeRedis()
    body = json.dumps({"event_type": "order.created"}).encode()
    with (
        patch.dict("os.environ", {"TEST_HOOKS_SECRET": SECRET}),
        patch(
            "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=redis
        ),
    ):
        resp = _signed_post(_client(), body)
    assert resp.status_code == 200


# ── delivery ───────────────────────────────────────────────────────


def test_signed_delivery_lands_on_the_engine_stream_and_a_replay_is_duplicate():
    redis = _FakeRedis()
    body = json.dumps({"event_type": "order.created", "id": 7}).encode()
    with (
        patch.dict("os.environ", {"TEST_HOOKS_SECRET": SECRET}),
        patch(
            "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=redis
        ),
    ):
        client = _client()
        first = _signed_post(client, body)
        replay = _signed_post(client, body)

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate"}

    stream_key = f"{ENGINE_STREAM_PREFIX}orders-events"
    assert list(redis.streams) == [stream_key]
    assert len(redis.streams[stream_key]) == 1
    envelope = redis.streams[stream_key][0]
    assert envelope["delivery_id"] == "dlv-1"
    assert envelope["event_type"] == "order.created"
    assert json.loads(envelope["payload"]) == {"event_type": "order.created", "id": 7}
    assert "robothor:webhook:delivery:orders:dlv-1" in redis.values


def test_each_delivery_logs_one_line_without_its_payload(caplog):
    redis = _FakeRedis()
    body = json.dumps({"event_type": "order.created", "note": PAYLOAD_MARKER}).encode()
    caplog.set_level(logging.INFO, logger="robothor.engine.hooks_server")
    with (
        patch.dict("os.environ", {"TEST_HOOKS_SECRET": SECRET}),
        patch(
            "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=redis
        ),
    ):
        client = _client()
        _signed_post(client, body)
        _signed_post(client, body)
        client.post(
            "/api/webhooks/orders",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    lines = [r.getMessage() for r in caplog.records if r.name == "robothor.engine.hooks_server"]
    assert len(lines) == 3
    assert "outcome=accepted" in lines[0]
    assert "outcome=duplicate" in lines[1]
    assert "outcome=refused" in lines[2]
    assert "status=401" in lines[2]
    assert all("channel=orders" in line for line in lines)
    assert not any(PAYLOAD_MARKER in r.getMessage() for r in caplog.records)


# ── configuration and entrypoint ───────────────────────────────────


def test_create_app_reads_channels_from_webhooks_config(tmp_path: Path, monkeypatch):
    override = tmp_path / "webhooks.yaml"
    override.write_text(
        textwrap.dedent("""\
            channels:
              mounted:
                stream: mounted
                secret_env: TEST_HOOKS_UNSET_SECRET
            """)
    )
    monkeypatch.setenv("ROBOTHOR_WEBHOOKS_CONFIG", str(override))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "empty-workspace"))
    from fastapi.testclient import TestClient

    client = TestClient(hooks_server.create_app())

    # Served (503: its secret is unset), not 404 Unknown channel.
    assert client.post("/api/webhooks/mounted", content=b"{}").status_code == 503


def test_main_serves_on_the_default_port(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HOOKS_PORT", raising=False)
    with (
        patch("uvicorn.run") as run,
        patch.object(hooks_server, "create_app", return_value=object()),
    ):
        hooks_server.main()
    assert run.call_args.kwargs["port"] == 18801


def test_main_serves_on_robothor_hooks_port(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HOOKS_PORT", "19123")
    with (
        patch("uvicorn.run") as run,
        patch.object(hooks_server, "create_app", return_value=object()),
    ):
        hooks_server.main()
    assert run.call_args.kwargs["port"] == 19123


def test_main_binds_all_interfaces_by_default(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HOOKS_HOST", raising=False)
    with (
        patch("uvicorn.run") as run,
        patch.object(hooks_server, "create_app", return_value=object()),
    ):
        hooks_server.main()
    assert run.call_args.kwargs["host"] == "0.0.0.0"  # noqa: S104


def test_main_binds_robothor_hooks_host(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_HOOKS_HOST", "127.0.0.1")
    with (
        patch("uvicorn.run") as run,
        patch.object(hooks_server, "create_app", return_value=object()),
    ):
        hooks_server.main()
    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_a_hostile_channel_path_never_reaches_the_log_raw(caplog):
    """CR/LF and ANSI escapes in the path cannot forge or colour a log line."""
    caplog.set_level(logging.INFO, logger="robothor.engine.hooks_server")
    hostile = "evil%0Awebhook%20delivery%20channel=orders%20outcome=accepted%1B[31m" + "x" * 500
    with patch(
        "robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock, return_value=None
    ):
        resp = _client().post(f"/api/webhooks/{hostile}", content=b"{}")
    assert resp.status_code == 404

    lines = [r.getMessage() for r in caplog.records if r.name == "robothor.engine.hooks_server"]
    assert len(lines) == 1
    line = lines[0]
    assert "channel=unknown " in line
    assert "\n" not in line
    assert "\r" not in line
    assert "\x1b" not in line
    assert "evil" not in line.split("reason=")[0]
    assert len(line) < 400
