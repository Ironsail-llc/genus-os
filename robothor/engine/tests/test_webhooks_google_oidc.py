"""The ``google_oidc`` webhook scheme: Pub/Sub push authenticated by a Google ID token."""

from __future__ import annotations

import base64
import json
import textwrap
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.webhooks import (
    _channel_stats,
    get_webhook_router,
    load_webhook_config,
)

if TYPE_CHECKING:
    from pathlib import Path

AUDIENCE = "https://agent.example.com/api/webhooks/gmail"
PUSH_SA = "push-sender@example.iam.gserviceaccount.com"

YAML = textwrap.dedent(f"""\
    channels:
      gmail:
        stream: gmail
        signature_scheme: google_oidc
        oidc_audience: "{AUDIENCE}"
        oidc_service_account: "{PUSH_SA}"
        event_type_prefix: "gmail.push"
      gmail-unconfigured:
        stream: gmail
        signature_scheme: google_oidc
""")


def _claims(**over: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": PUSH_SA,
        "email_verified": True,
    }
    claims.update(over)
    return claims


class FakeVerifier:
    """Stands in for ``google.oauth2.id_token.verify_oauth2_token``.

    A token is the JSON of its claims; like the real one it raises
    ``ValueError`` when the audience is not the one asked for.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, token: str, request: Any, audience: str | None = None) -> dict[str, Any]:
        self.calls.append((token, audience))
        claims: dict[str, Any] = json.loads(token)
        if audience is not None and claims.get("aud") != audience:
            raise ValueError("Token has wrong audience")
        return claims


def _push_body(
    data: dict[str, Any] | None = None, *, message_id: str = "m-1", raw_data: str | None = None
) -> bytes:
    if data is None:
        data = {"emailAddress": "agent@example.com", "historyId": 4242}
    encoded = raw_data if raw_data is not None else base64.b64encode(json.dumps(data).encode())
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    return json.dumps(
        {
            "message": {
                "data": encoded,
                "messageId": message_id,
                "publishTime": "2026-01-01T00:00:00.000Z",
                "attributes": {},
            },
            "subscription": "projects/example-project/subscriptions/gmail-push",
        }
    ).encode()


@pytest.fixture()
def oidc_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    app = FastAPI()
    app.include_router(get_webhook_router(load_webhook_config(path)))
    _channel_stats.clear()
    yield TestClient(app)
    _channel_stats.clear()


@pytest.fixture()
def verifier():
    fake = FakeVerifier()
    with patch("google.oauth2.id_token.verify_oauth2_token", fake):
        yield fake


@pytest.fixture()
def redis():
    r = AsyncMock()
    r.set = AsyncMock(return_value=True)
    with patch("robothor.engine.webhooks._get_async_redis", new_callable=AsyncMock) as get_r:
        get_r.return_value = r
        yield r


def _bearer(claims: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {json.dumps(claims)}", "Content-Type": "application/json"}


def test_config_loads_the_oidc_fields(tmp_path: Path) -> None:
    path = tmp_path / "webhooks.yaml"
    path.write_text(YAML)
    ch = load_webhook_config(path).channels["gmail"]
    assert ch.signature_scheme == "google_oidc"
    assert ch.oidc_audience == AUDIENCE
    assert ch.oidc_service_account == PUSH_SA
    assert ch.secret_env == ""


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="e-1")
def test_valid_token_publishes_the_decoded_message(
    mock_publish, _rate, oidc_client, verifier, redis
) -> None:
    resp = oidc_client.post("/api/webhooks/gmail", content=_push_body(), headers=_bearer(_claims()))
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_type"] == "gmail.push"
    # The token was checked against the channel's audience, without a secret_env.
    assert verifier.calls and verifier.calls[0][1] == AUDIENCE

    stream, event_type, payload = mock_publish.call_args.args
    assert stream == "gmail"
    assert event_type == "gmail.push"
    assert payload["emailAddress"] == "agent@example.com"
    assert payload["historyId"] == 4242
    assert payload["pubsub"]["message_id"] == "m-1"
    assert payload["pubsub"]["subscription"].endswith("/gmail-push")
    # The Pub/Sub messageId is the delivery id: a redelivery is a duplicate.
    assert mock_publish.call_args.kwargs["delivery_id"] == "m-1"
    redis.set.assert_awaited_once_with("robothor:webhook:delivery:gmail:m-1", 1, nx=True, ex=86400)


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_redelivered_message_id_is_a_duplicate(
    mock_publish, _rate, oidc_client, verifier, redis
) -> None:
    redis.set = AsyncMock(return_value=None)
    resp = oidc_client.post("/api/webhooks/gmail", content=_push_body(), headers=_bearer(_claims()))
    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate"}
    mock_publish.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(_bearer(_claims(aud="https://other.example.com/")), id="wrong-audience"),
        pytest.param(_bearer(_claims(email="intruder@example.com")), id="wrong-service-account"),
        pytest.param(_bearer(_claims(email_verified=False)), id="unverified-email"),
        pytest.param(_bearer({k: v for k, v in _claims().items() if k != "email"}), id="no-email"),
        pytest.param({"Content-Type": "application/json"}, id="missing-header"),
        pytest.param(
            {"Authorization": "Basic abc", "Content-Type": "application/json"}, id="not-bearer"
        ),
    ],
)
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_rejected_tokens_are_401(
    mock_publish, _rate, headers, oidc_client, verifier, redis
) -> None:
    resp = oidc_client.post("/api/webhooks/gmail", content=_push_body(), headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "Invalid signature"}
    mock_publish.assert_not_called()


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_verifier_failure_of_any_kind_is_401(mock_publish, _rate, oidc_client, redis) -> None:
    def broken(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("certificate fetch failed")

    with patch("google.oauth2.id_token.verify_oauth2_token", broken):
        resp = oidc_client.post(
            "/api/webhooks/gmail", content=_push_body(), headers=_bearer(_claims())
        )
    assert resp.status_code == 401
    mock_publish.assert_not_called()


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_channel_without_audience_or_account_is_503(
    mock_publish, _rate, oidc_client, verifier, redis
) -> None:
    resp = oidc_client.post(
        "/api/webhooks/gmail-unconfigured", content=_push_body(), headers=_bearer(_claims())
    )
    assert resp.status_code == 503
    assert verifier.calls == []
    mock_publish.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"subscription": "s"}', id="no-message"),
        pytest.param(_push_body(raw_data="%%% not base64 %%%"), id="bad-base64"),
    ],
)
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock)
def test_malformed_push_envelope_is_400(
    mock_publish, _rate, body, oidc_client, verifier, redis
) -> None:
    resp = oidc_client.post("/api/webhooks/gmail", content=body, headers=_bearer(_claims()))
    assert resp.status_code == 400
    mock_publish.assert_not_called()


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="e-2")
def test_non_json_data_is_kept_as_text(mock_publish, _rate, oidc_client, verifier, redis) -> None:
    body = _push_body(raw_data=base64.b64encode(b"plain words").decode())
    resp = oidc_client.post("/api/webhooks/gmail", content=body, headers=_bearer(_claims()))
    assert resp.status_code == 200, resp.text
    payload = mock_publish.call_args.args[2]
    assert payload["data"] == "plain words"
    assert payload["pubsub"]["message_id"] == "m-1"


@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="e-3")
def test_hmac_channels_still_need_their_secret(_publish, _rate, tmp_path: Path) -> None:
    """The 503-if-unset rule is unchanged for every other scheme."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    path = tmp_path / "webhooks.yaml"
    path.write_text("channels:\n  plain:\n    stream: webhook\n    secret_env: UNSET_SECRET_X\n")
    app = FastAPI()
    app.include_router(get_webhook_router(load_webhook_config(path)))
    resp = TestClient(app).post("/api/webhooks/plain", content=b"{}")
    assert resp.status_code == 503
