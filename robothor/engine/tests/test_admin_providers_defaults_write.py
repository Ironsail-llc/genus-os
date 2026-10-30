"""``PUT /api/admin/defaults``: the engine writes the fleet default model block.

The bridge used to write ``_defaults.yaml`` itself and then ask the engine to
reload. That only works where both processes share a filesystem: in a
container deployment the bridge has no workspace volume, so the write landed
in the bridge container (or failed) and the engine never saw it. The engine is
the process that reads the file, so it is the process that writes it.

What these tests hold down:

* the write lands in the engine's manifest dir and the read route then
  returns it, with no restart and no second call;
* only the ``model.primary`` / ``model.fallbacks`` keys change;
* a model the engine's catalog does not list is refused before anything is
  written;
* the write is atomic: a failure mid-write leaves the old file intact and no
  temp file behind;
* the route sits behind the same ``engine:control`` gate as the rest of
  ``/api/admin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.auth import tokens
from robothor.engine import admin_providers
from robothor.engine import config as defaults_config
from robothor.engine.tests.test_admin_providers import _make_app

if TYPE_CHECKING:
    from pathlib import Path

CATALOG = [
    {"id": "openrouter/openai/gpt-5.4", "provider": "openrouter", "source": "registry"},
    {"id": "ollama_chat/qwen3:8b", "provider": "ollama_chat", "source": "registry"},
]


@pytest.fixture
def agents(tmp_path, monkeypatch) -> Path:
    """A throwaway manifest dir: ``_defaults.yaml`` is instance data and the
    real one is never read or written by a test."""
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: directory)
    monkeypatch.setattr(admin_providers, "known_models", lambda: list(CATALOG))
    defaults_config.reset_defaults_cache()
    yield directory
    defaults_config.reset_defaults_cache()


def _client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


def _write(directory: Path, data: dict) -> Path:
    path = directory / "_defaults.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


class TestWrite:
    def test_it_writes_the_model_block_into_the_engines_manifest_dir(self, agents) -> None:
        response = _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": ["ollama_chat/qwen3:8b"]},
        )
        assert response.status_code == 200
        written = yaml.safe_load((agents / "_defaults.yaml").read_text())
        assert written["model"] == {
            "primary": "openrouter/openai/gpt-5.4",
            "fallbacks": ["ollama_chat/qwen3:8b"],
        }

    def test_it_answers_with_the_new_defaults_in_the_read_shape(self, agents) -> None:
        body = (
            _client()
            .put(
                "/api/admin/defaults",
                json={"model": "openrouter/openai/gpt-5.4", "fallbacks": ["ollama_chat/qwen3:8b"]},
            )
            .json()
        )
        assert body == {
            "primary": "openrouter/openai/gpt-5.4",
            "fallbacks": ["ollama_chat/qwen3:8b"],
        }

    def test_the_read_route_returns_what_was_just_written(self, agents) -> None:
        """The engine had cached the old block; the write must reload it in
        the same request, or "saved" and "in use" are different states."""
        _write(agents, {"model": {"primary": "old/model", "fallbacks": []}})
        client = _client()
        assert client.get("/api/admin/defaults").json()["primary"] == "old/model"

        client.put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert client.get("/api/admin/defaults").json() == {
            "primary": "openrouter/openai/gpt-5.4",
            "fallbacks": [],
        }

    def test_the_write_resets_the_defaults_cache(self, agents, monkeypatch) -> None:
        resets: list[bool] = []
        real_reset = defaults_config.reset_defaults_cache

        def _spy() -> None:
            resets.append(True)
            real_reset()

        monkeypatch.setattr(defaults_config, "reset_defaults_cache", _spy)
        _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert resets, "the engine kept running on the block it had cached"

    def test_it_preserves_every_other_key(self, agents) -> None:
        path = _write(
            agents,
            {
                "delivery": {"mode": "summary"},
                "model": {"primary": "old/model", "temperature": 0.4},
                "v2": {"sandbox": {"enabled": True}},
            },
        )
        _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        written = yaml.safe_load(path.read_text())
        assert written["delivery"] == {"mode": "summary"}
        assert written["v2"] == {"sandbox": {"enabled": True}}
        assert written["model"]["temperature"] == 0.4, "an unrelated model key was dropped"
        assert written["model"]["primary"] == "openrouter/openai/gpt-5.4"

    def test_it_creates_the_file_when_there_is_none(self, agents) -> None:
        response = _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert response.status_code == 200
        assert (agents / "_defaults.yaml").exists()

    def test_fallbacks_default_to_none(self, agents) -> None:
        response = _client().put("/api/admin/defaults", json={"model": "openrouter/openai/gpt-5.4"})
        assert response.status_code == 200
        assert response.json()["fallbacks"] == []


class TestValidation:
    def test_an_unknown_primary_model_is_refused_with_the_same_message(self, agents) -> None:
        response = _client().put("/api/admin/defaults", json={"model": "made/up", "fallbacks": []})
        assert response.status_code == 422
        assert response.json()["detail"] == "unknown model id(s): made/up"
        assert not (agents / "_defaults.yaml").exists()

    def test_an_unknown_fallback_is_refused(self, agents) -> None:
        response = _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": ["z/z", "a/a"]},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "unknown model id(s): a/a, z/z"
        assert not (agents / "_defaults.yaml").exists()

    def test_an_empty_model_is_refused(self, agents) -> None:
        response = _client().put("/api/admin/defaults", json={"model": "", "fallbacks": []})
        assert response.status_code == 422
        assert not (agents / "_defaults.yaml").exists()

    def test_a_file_that_is_not_a_mapping_is_not_overwritten(self, agents) -> None:
        path = agents / "_defaults.yaml"
        path.write_text("- just\n- a list\n")
        response = _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert response.status_code == 500
        assert path.read_text() == "- just\n- a list\n"


class TestAtomicity:
    def test_the_write_leaves_no_temp_file_behind(self, agents) -> None:
        _client().put(
            "/api/admin/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert [p.name for p in agents.iterdir()] == ["_defaults.yaml"]

    def test_a_failed_write_leaves_the_old_file_and_no_partial_one(self, agents) -> None:
        """A torn ``_defaults.yaml`` is an unparseable manifest layer every
        agent inherits. Temp file, then rename: a failure before the rename
        must leave the previous file exactly as it was."""
        path = _write(agents, {"model": {"primary": "old/model", "fallbacks": []}})
        before = path.read_text()

        def _torn(data, handle, **kwargs):
            handle.write("model:\n  primary: half")
            raise OSError("disk full")

        with patch("yaml.safe_dump", side_effect=_torn):
            response = _client().put(
                "/api/admin/defaults",
                json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
            )
        assert response.status_code >= 500
        assert path.read_text() == before
        assert [p.name for p in agents.iterdir()] == ["_defaults.yaml"]


@pytest.fixture
def secure_engine(monkeypatch, engine_config, tmp_path):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "engine-auth-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()
    monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: engine_config.manifest_dir)
    monkeypatch.setattr(admin_providers, "known_models", lambda: list(CATALOG))

    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock()

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.engine.chat.load_all_sessions", return_value={}),
    ):
        app = create_health_app(engine_config, runner=runner)
    yield TestClient(app, raise_server_exceptions=False), engine_config
    tokens.reset_signing_key_cache()


def _service_token(tenant_id: str, *scopes: str) -> dict[str, str]:
    token = tokens.issue_service_token(
        "bridge-worker", tenant_id, audience="genus-engine", scopes=scopes
    )
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    BODY = {"model": "openrouter/openai/gpt-5.4", "fallbacks": []}

    def test_the_route_demands_engine_control(self) -> None:
        from robothor.engine.auth import required_scope

        assert required_scope("PUT", "/api/admin/defaults") == "engine:control"

    def test_an_unauthenticated_write_is_refused(self, secure_engine) -> None:
        client, config = secure_engine
        response = client.put("/api/admin/defaults", json=self.BODY)
        assert response.status_code == 401
        assert not (config.manifest_dir / "_defaults.yaml").exists()

    def test_a_token_without_control_scope_is_refused(self, secure_engine) -> None:
        client, config = secure_engine
        response = client.put(
            "/api/admin/defaults",
            json=self.BODY,
            headers=_service_token(config.tenant_id, "engine:read", "engine:write"),
        )
        assert response.status_code == 403
        assert not (config.manifest_dir / "_defaults.yaml").exists()

    def test_a_control_scoped_token_writes(self, secure_engine) -> None:
        client, config = secure_engine
        response = client.put(
            "/api/admin/defaults",
            json=self.BODY,
            headers=_service_token(config.tenant_id, "engine:control"),
        )
        assert response.status_code == 200
        assert (config.manifest_dir / "_defaults.yaml").exists()
