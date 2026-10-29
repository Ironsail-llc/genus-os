"""``docs/webhooks.d/*.yaml`` drop-ins extend ``docs/webhooks.yaml``.

A plugin owns its own drop-in file, so two plugins can each add a channel
without fighting over the one shared file. Pinned here: every drop-in is
merged after the base file in name order, a channel declared twice is an
error naming both files, a missing directory is fine, and the router serves a
drop-in channel exactly like a base one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.webhooks import _channel_stats, get_webhook_router, load_webhook_config

if TYPE_CHECKING:
    from pathlib import Path


def _channel(name: str, secret_env: str = "TEST_DROPIN_SECRET") -> str:
    return textwrap.dedent(f"""\
        channels:
          {name}:
            stream: {name}
            secret_env: {secret_env}
            event_type_field: event_type
        """)


@pytest.fixture()
def docs(tmp_path: Path) -> Path:
    path = tmp_path / "docs"
    path.mkdir()
    (path / "webhooks.yaml").write_text(_channel("github"))
    return path


def test_drop_ins_merge_after_the_base_file(docs: Path) -> None:
    dropins = docs / "webhooks.d"
    dropins.mkdir()
    (dropins / "beta-plugin.yaml").write_text(_channel("beta"))
    (dropins / "alpha-plugin.yaml").write_text(_channel("alpha"))
    (dropins / "notes.txt").write_text("not yaml, not read\n")

    cfg = load_webhook_config(docs / "webhooks.yaml")

    assert list(cfg.channels) == ["github", "alpha", "beta"]
    assert cfg.channels["alpha"].stream == "alpha"
    assert cfg.channels["beta"].secret_env == "TEST_DROPIN_SECRET"


def test_a_missing_drop_in_directory_is_fine(docs: Path) -> None:
    cfg = load_webhook_config(docs / "webhooks.yaml")
    assert list(cfg.channels) == ["github"]


def test_drop_ins_load_without_a_base_file(tmp_path: Path) -> None:
    dropins = tmp_path / "webhooks.d"
    dropins.mkdir()
    (dropins / "alpha-plugin.yaml").write_text(_channel("alpha"))
    cfg = load_webhook_config(tmp_path / "webhooks.yaml")
    assert list(cfg.channels) == ["alpha"]


def test_a_drop_in_redeclaring_a_base_channel_names_both_files(docs: Path) -> None:
    dropins = docs / "webhooks.d"
    dropins.mkdir()
    (dropins / "alpha-plugin.yaml").write_text(_channel("github"))
    with pytest.raises(ValueError, match="github") as caught:
        load_webhook_config(docs / "webhooks.yaml")
    message = str(caught.value)
    assert "webhooks.yaml" in message and "alpha-plugin.yaml" in message


def test_two_drop_ins_declaring_one_channel_names_both_files(docs: Path) -> None:
    dropins = docs / "webhooks.d"
    dropins.mkdir()
    (dropins / "alpha-plugin.yaml").write_text(_channel("shared"))
    (dropins / "beta-plugin.yaml").write_text(_channel("shared"))
    with pytest.raises(ValueError, match="shared") as caught:
        load_webhook_config(docs / "webhooks.yaml")
    message = str(caught.value)
    assert "alpha-plugin.yaml" in message and "beta-plugin.yaml" in message


@pytest.fixture()
def _clear_stats():
    _channel_stats.clear()
    yield
    _channel_stats.clear()


@patch.dict("os.environ", {"TEST_DROPIN_SECRET": "dropin-secret"})
@patch("robothor.engine.webhooks._check_rate_limit", new_callable=AsyncMock, return_value=True)
@patch("robothor.engine.webhooks._publish_to_stream", new_callable=AsyncMock, return_value="evt-1")
def test_the_router_serves_a_drop_in_channel(
    mock_publish, mock_rate, tmp_path: Path, monkeypatch, _clear_stats
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    workspace = tmp_path / "ws"
    dropins = workspace / "docs" / "webhooks.d"
    dropins.mkdir(parents=True)
    (workspace / "docs" / "webhooks.yaml").write_text(_channel("github", "UNSET_SECRET"))
    (dropins / "alpha-plugin.yaml").write_text(_channel("alpha"))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

    app = FastAPI()
    app.include_router(get_webhook_router())
    client = TestClient(app)

    payload = json.dumps({"event_type": "alpha.ping"}).encode()
    signature = hmac.new(b"dropin-secret", payload, hashlib.sha256).hexdigest()
    resp = client.post(
        "/api/webhooks/alpha",
        content=payload,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["event_type"] == "alpha.ping"
    mock_publish.assert_called_once()
