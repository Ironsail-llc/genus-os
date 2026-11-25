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


def test_every_engine_listens_on_the_datadog_stream() -> None:
    from robothor.engine.hooks import platform_streams

    assert "datadog" in platform_streams()
