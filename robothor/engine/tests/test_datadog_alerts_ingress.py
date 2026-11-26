"""Datadog monitor webhooks end to end: the ``header_secret`` scheme, the stream, the record.

Datadog's webhook integration cannot sign a body; it sends the operator's
custom headers. The ``header_secret`` scheme checks one of them against the
channel's secret in constant time. An accepted delivery is published to the
``datadog`` stream like any webhook, and the engine that owns it records one
``datadog.alert_received`` audit event in the tenant the ``org:`` tag names.
"""

from __future__ import annotations

import json
import logging
import textwrap
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.engine.hooks import STREAM_PREFIX, EventHooks
from robothor.engine.webhooks import (
    SIGNATURE_SCHEMES,
    _channel_stats,
    _verify_header_secret,
    get_webhook_router,
    load_webhook_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from robothor.engine.config import EngineConfig

SECRET = "dd-shared-secret-0123456789"
HEADER = "X-Genus-Webhook-Secret"
CHILD = "acme-child-a"
STREAM = f"{STREAM_PREFIX}datadog"

YAML = textwrap.dedent(f"""\
    channels:
      datadog:
        stream: datadog
        secret_env: TEST_DD_WEBHOOK_SECRET
        signature_scheme: header_secret
        signature_header: {HEADER}
        event_type_prefix: datadog.alert
      datadog-default-header:
        stream: datadog
        secret_env: TEST_DD_WEBHOOK_SECRET
        signature_scheme: header_secret
""")

PAYLOAD = {
    "monitor_id": "12345",
    "event_id": "777",
    "title": "[Triggered] shop-api error rate high",
    "transition": "Triggered",
    "status": "Alert",
    "priority": "P2",
    "query": "",
    "tags": f"env:prod,org:{CHILD},service:shop-api",
    "link": "https://app.example.com/monitors/12345",
    "date": "1764000000000",
    "hostname": "",
}


# ── the scheme ─────────────────────────────────────────────────────


def test_scheme_is_known() -> None:
    assert "header_secret" in SIGNATURE_SCHEMES


def test_config_loads(tmp_path: Path, caplog) -> None:
    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    with caplog.at_level(logging.WARNING, logger="robothor.engine.webhooks"):
        cfg = load_webhook_config(path)
    assert cfg.channels["datadog"].signature_scheme == "header_secret"
    assert "unknown signature_scheme" not in caplog.text


class TestVerifyHeaderSecret:
    def test_match(self) -> None:
        assert _verify_header_secret(SECRET, SECRET) == (True, "")

    def test_mismatch(self) -> None:
        assert _verify_header_secret("wrong", SECRET) == (False, "secret mismatch")

    def test_missing(self) -> None:
        assert _verify_header_secret("", SECRET) == (False, "missing secret header")

    def test_no_configured_secret_never_matches(self) -> None:
        assert _verify_header_secret("", "")[0] is False

    def test_non_ascii_is_a_mismatch_not_an_error(self) -> None:
        assert _verify_header_secret("\xe9" * 10, SECRET) == (False, "secret mismatch")

    def test_compared_in_constant_time(self) -> None:
        import hmac

        with patch("robothor.engine.webhooks.hmac.compare_digest", wraps=hmac.compare_digest) as cd:
            _verify_header_secret("wrong", SECRET)
        cd.assert_called_once()


# ── the endpoint ───────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TEST_DD_WEBHOOK_SECRET", SECRET)
    _channel_stats.clear()
    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    app = FastAPI()
    app.include_router(get_webhook_router(load_webhook_config(path)))
    yield TestClient(app)
    _channel_stats.clear()


@pytest.fixture()
def published():
    """Capture what the receiver would add to the stream, and allow the rate limit."""
    sent: list[dict[str, Any]] = []

    async def _publish(stream, event_type, payload, *, delivery_id=""):
        sent.append({"stream": stream, "event_type": event_type, "payload": payload})
        return f"e-{len(sent)}"

    with (
        patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock) as rate,
        patch("robothor.engine.webhooks._publish_to_stream", side_effect=_publish),
    ):
        rate.return_value = True
        yield sent


def _post(client, body: bytes, headers: dict[str, str] | None = None, channel: str = "datadog"):
    return client.post(
        f"/api/webhooks/{channel}",
        content=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def test_right_secret_is_accepted_and_published(client, published) -> None:
    resp = _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_type"] == "datadog.alert"
    assert published == [{"stream": "datadog", "event_type": "datadog.alert", "payload": PAYLOAD}]


def test_default_header_name(client, published) -> None:
    resp = _post(
        client, json.dumps(PAYLOAD).encode(), {"X-Webhook-Secret": SECRET}, "datadog-default-header"
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({HEADER: "not-the-secret-value"}, "secret mismatch"),
        ({}, "missing secret header"),
        ({"X-Webhook-Secret": SECRET}, "missing secret header"),  # the wrong header
    ],
)
def test_wrong_or_missing_secret_is_401_and_nothing_is_published(
    client, published, caplog, headers, reason
) -> None:
    with caplog.at_level(logging.WARNING, logger="robothor.engine.webhooks"):
        resp = _post(client, json.dumps(PAYLOAD).encode(), headers)
    assert resp.status_code == 401
    assert published == []
    refusals = [r.getMessage() for r in caplog.records if "datadog" in r.getMessage()]
    assert len(refusals) == 1, refusals
    assert reason in refusals[0]
    assert "not-the-secret-value" not in caplog.text
    assert SECRET not in caplog.text


def test_unconfigured_secret_is_503(client, published, monkeypatch) -> None:
    monkeypatch.delenv("TEST_DD_WEBHOOK_SECRET")
    resp = _post(client, json.dumps(PAYLOAD).encode(), {HEADER: ""})
    assert resp.status_code == 503
    assert published == []


def test_invalid_json_is_400(client, published) -> None:
    resp = _post(client, b"{not json", {HEADER: SECRET})
    assert resp.status_code == 400
    assert published == []


# ── the engine records it ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def datadog_in_use(monkeypatch):
    """The engine records alerts once the instance uses Datadog (a scope map is set)."""
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", '{"acme-child-a": {"logs": "service:shop-api"}}')
    reset_settings()
    yield
    reset_settings()


class FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.acked: list[tuple[str, str, Any]] = []
        self.added: list[tuple[str, dict[str, Any]]] = []

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.keys.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.keys else 0

    async def get(self, key: str) -> bytes | None:
        value = self.keys.get(key)
        return value.encode() if isinstance(value, str) else value

    async def xack(self, stream: str, group: str, msg_id: Any) -> int:
        self.acked.append((stream, group, msg_id))
        return 1

    async def xadd(self, stream: str, fields: dict[str, Any], **kw: Any) -> bytes:
        self.added.append((stream, fields))
        return b"9-0"


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_event(self, event_type: str, action: str, **kw: Any) -> dict[str, Any]:
        self.rows.append({"event_type": event_type, "action": action, **kw})
        return {"id": len(self.rows), "timestamp": "2026-11-25T00:00:00+00:00"}


@pytest.fixture()
def audit():
    fake = FakeAudit()
    tenants = {CHILD: {"id": CHILD, "active": True}}
    with (
        patch("robothor.audit.logger.log_event", side_effect=fake.log_event),
        patch("robothor.crm.dal.get_tenant", side_effect=lambda t: tenants.get(t)),
    ):
        yield fake


def _root_hooks(engine_config: EngineConfig) -> tuple[EventHooks, MagicMock]:
    from dataclasses import replace

    runner = MagicMock()
    runner.execute = AsyncMock()
    hooks = EventHooks(replace(engine_config, tenant_id=DEFAULT_TENANT), runner=runner)
    hooks._event_triggers = {"datadog": []}
    hooks._prefixed_to_bare = {STREAM: "datadog"}
    return hooks, runner


def _envelope(published_event: dict[str, Any]) -> dict[bytes, bytes]:
    fields = {
        "type": published_event["event_type"],
        "event_type": published_event["event_type"],
        "source": "webhook",
        "payload": json.dumps(published_event["payload"]),
        "tenant_id": DEFAULT_TENANT,
    }
    return {k.encode(): v.encode() for k, v in fields.items()}


@pytest.fixture()
def no_delivery():
    with (
        patch("robothor.engine.alerts.alert", new_callable=AsyncMock) as alert,
        patch("robothor.engine.alerts._write_notification", new_callable=AsyncMock) as notify,
        patch("robothor.engine.alerts._send_telegram", new_callable=AsyncMock) as telegram,
    ):
        yield (alert, notify, telegram)


@pytest.mark.asyncio
async def test_an_accepted_delivery_is_recorded_in_the_tagged_tenant(
    client, published, audit, no_delivery, engine_config
) -> None:
    resp = _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    assert resp.status_code == 200
    hooks, runner = _root_hooks(engine_config)
    redis = FakeRedis()

    await hooks._handle_event(STREAM, b"1-0", _envelope(published[0]), redis, "engine")

    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row["event_type"] == "datadog.alert_received"
    assert row["tenant_id"] == CHILD
    assert row["details"]["mode"] == "dry_run"
    assert row["details"]["would_notify"]
    assert row["details"]["would_run"]
    assert redis.acked == [(STREAM, "engine", b"1-0")]
    runner.execute.assert_not_called()
    for watched in no_delivery:
        watched.assert_not_called()


@pytest.mark.asyncio
async def test_a_redelivered_alert_is_recorded_once(
    client, published, audit, no_delivery, engine_config
) -> None:
    body = json.dumps(PAYLOAD).encode()
    _post(client, body, {HEADER: SECRET})
    _post(client, body, {HEADER: SECRET})
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()

    for n, event in enumerate(published):
        await hooks._handle_event(STREAM, f"{n}-0".encode(), _envelope(event), redis, "engine")

    assert len(audit.rows) == 1
    assert len(redis.acked) == 2


@pytest.mark.asyncio
async def test_recovery_after_trigger_is_a_second_event(
    client, published, audit, no_delivery, engine_config
) -> None:
    _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    recovered = {**PAYLOAD, "transition": "Recovered", "date": "1764000600000"}
    _post(client, json.dumps(recovered).encode(), {HEADER: SECRET})
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()

    for n, event in enumerate(published):
        await hooks._handle_event(STREAM, f"{n}-0".encode(), _envelope(event), redis, "engine")

    assert [r["details"]["transition"] for r in audit.rows] == ["triggered", "recovered"]
    assert {r["details"]["monitor_id"] for r in audit.rows} == {"12345"}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"[]", b'"text"', b"null"])
async def test_an_empty_or_odd_body_is_accepted_with_empty_fields(
    client, published, audit, no_delivery, engine_config, body
) -> None:
    resp = _post(client, body, {HEADER: SECRET})
    assert resp.status_code == 200, resp.text
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()

    await hooks._handle_event(STREAM, b"1-0", _envelope(published[0]), redis, "engine")

    assert len(audit.rows) == 1
    assert audit.rows[0]["tenant_id"] == DEFAULT_TENANT
    assert audit.rows[0]["details"]["routing"] == "untagged"


@pytest.mark.asyncio
async def test_a_failed_record_is_dead_lettered_not_lost(
    client, published, no_delivery, engine_config
) -> None:
    _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()
    with (
        patch("robothor.audit.logger.log_event", return_value=None),
        patch("robothor.crm.dal.get_tenant", return_value=None),
    ):
        await hooks._handle_event(STREAM, b"1-0", _envelope(published[0]), redis, "engine")

    assert [s for s, _ in redis.added] == [f"{STREAM}:dlq"]
    assert redis.acked == [(STREAM, "engine", b"1-0")]


@pytest.mark.asyncio
async def test_a_child_engine_does_not_record_the_roots_event(
    client, published, audit, no_delivery, engine_config
) -> None:
    from dataclasses import replace

    _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    child = EventHooks(
        replace(engine_config, tenant_id=CHILD, event_group=f"engine-{CHILD}"), runner=MagicMock()
    )
    child._event_triggers = {"datadog": []}
    child._prefixed_to_bare = {STREAM: "datadog"}

    await child._handle_event(
        STREAM, b"1-0", _envelope(published[0]), FakeRedis(), f"engine-{CHILD}"
    )

    assert audit.rows == []


@pytest.mark.asyncio
async def test_an_instance_without_datadog_leaves_a_datadog_stream_alone(
    client, published, audit, no_delivery, engine_config, monkeypatch
) -> None:
    """A stream an instance already names ``datadog`` for its own hooks is not
    taken over: with no scope map there is no record and no dead letter."""
    from robothor.settings import reset_settings

    _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    monkeypatch.delenv("ROBOTHOR_DATADOG_SCOPES", raising=False)
    reset_settings()
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()
    with patch(
        "robothor.integrations.datadog.alerts.handle_alert_event", new_callable=AsyncMock
    ) as handled:
        await hooks._handle_event(STREAM, b"1-0", _envelope(published[0]), redis, "engine")

    handled.assert_not_called()
    assert audit.rows == []
    assert redis.added == []
    assert redis.acked == [(STREAM, "engine", b"1-0")]


def test_an_instance_using_datadog_listens_on_the_datadog_stream(monkeypatch) -> None:
    from robothor.engine.hooks import platform_streams
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", '{"acme-child-a": {"logs": "service:shop-api"}}')
    reset_settings()
    try:
        assert "datadog" in platform_streams()
    finally:
        reset_settings()


def test_an_instance_without_datadog_adds_no_stream(monkeypatch) -> None:
    from robothor.engine.hooks import platform_streams
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_DATADOG_SCOPES", raising=False)
    reset_settings()
    try:
        assert "datadog" not in platform_streams()
    finally:
        reset_settings()


@pytest.mark.asyncio
async def test_the_consumer_creates_the_datadog_group_when_in_use(
    engine_config, monkeypatch
) -> None:
    """``start()`` subscribes to the platform stream on top of the manifests' streams."""
    import asyncio

    from robothor.engine.tests.test_hooks_tenancy import FakeStreams

    fake = FakeStreams()
    hooks, _ = _root_hooks(engine_config)
    fake.on_idle = hooks._stop_event.set
    with (
        patch("robothor.engine.hooks.platform_streams", return_value=("datadog",)),
        patch("redis.asyncio.Redis", return_value=fake),
        patch("robothor.engine.hooks.build_event_triggers", return_value={}),
        patch("robothor.engine.leader.ha_leader_enabled", return_value=False),
        patch("robothor.crm.dal.get_tenant", return_value={"id": DEFAULT_TENANT}),
    ):
        await asyncio.wait_for(hooks.start(), timeout=5)

    assert (STREAM, "engine") in fake.created


@pytest.mark.asyncio
async def test_a_replayed_message_after_a_crash_is_recorded(
    client, published, audit, no_delivery, engine_config
) -> None:
    """The consumer passes the stream entry id; a claim it left is its own to finish."""
    from robothor.integrations.datadog import alerts as dd

    _post(client, json.dumps(PAYLOAD).encode(), {HEADER: SECRET})
    hooks, _ = _root_hooks(engine_config)
    redis = FakeRedis()
    key = dd._dedup_key(dd.parse_alert(PAYLOAD), PAYLOAD)
    redis.keys[key] = b"5-0"  # left by this message before the engine died

    await hooks._handle_event(STREAM, b"5-0", _envelope(published[0]), redis, "engine")

    assert len(audit.rows) == 1
    assert redis.keys[key] == "done"
