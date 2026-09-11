"""Provider keys, models and defaults, from the browser.

The bridge does not re-implement any of this: the engine owns the key pool,
the model registry and the process litellm resolves credentials from, so the
bridge's whole job here is to be the operator-gated, audited front door — and
to write the vault, which is the one thing the browser must be able to cause
and must never be able to read back.

What these tests hold down:

* the operator gate on every route, reads included — the listing enumerates
  which credentials this appliance holds;
* a key goes IN and never comes OUT, in a response or in a log line;
* the defaults write preserves the manifest keys it was not asked about, and
  refuses a model the engine does not know.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path  # noqa: TC003
from unittest.mock import patch

import pytest
import yaml

FAKE_KEY = "sk-test-9999999999999999999999999"

ENGINE_PROVIDERS = {
    "providers": [
        {
            "id": "openrouter",
            "label": "OpenRouter",
            "configured": True,
            "slots": [
                {
                    "position": 1,
                    "source": "vault",
                    "fingerprint": "sha256:ab12cd34",
                    "state": "active",
                    "updated_at": None,
                }
            ],
            "env_var": "OPENROUTER_API_KEY",
            "default_model": "openrouter/openai/gpt-5.4",
        }
    ]
}

ENGINE_MODELS = {
    "models": [
        {
            "id": "openrouter/openai/gpt-5.4",
            "provider": "openrouter",
            "context_window": 400000,
            "supports_thinking": False,
            "supports_tools": True,
            "source": "registry",
        },
        {
            "id": "ollama_chat/qwen3:8b",
            "provider": "ollama_chat",
            "context_window": 40960,
            "supports_thinking": True,
            "supports_tools": True,
            "source": "registry",
        },
    ]
}


class FakeEngine:
    """Stands in for the engine's ``/api/admin`` surface.

    Records every call so a test can assert that a vault write is followed by
    a reload — without that, a key lands in the database and the running
    engine keeps using the one it started with, which is the "configured but
    not working" state this whole PR exists to remove.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: dict[tuple[str, str], tuple[int, dict]] = {
            ("GET", "/api/admin/providers"): (200, ENGINE_PROVIDERS),
            ("GET", "/api/admin/models"): (200, ENGINE_MODELS),
            ("POST", "/api/admin/secrets/reload"): (200, {"reloaded": ["openrouter"], "slots": 1}),
        }

    async def __call__(self, method, path, *, json=None, timeout=30):
        self.calls.append((method, path, json))
        if path.endswith("/test"):
            return 200, {
                "ok": True,
                "model": "openrouter/openai/gpt-5.4",
                "latency_ms": 42,
                "error_class": None,
                "message": "OpenRouter answered in 42ms.",
            }
        return self.responses[(method, path)]

    @property
    def reload_count(self) -> int:
        return sum(1 for m, p, _ in self.calls if p == "/api/admin/secrets/reload")


class FakeVault:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.deleted: list[str] = []

    def set(self, key: str, value: str, **kwargs) -> None:
        self.written[key] = value

    def delete(self, key: str, **kwargs) -> bool:
        self.deleted.append(key)
        return key in self.written


@pytest.fixture
def fake_engine():
    engine = FakeEngine()
    with patch("routers.providers.engine_request", new=engine):
        yield engine


@pytest.fixture
def fake_vault():
    vault = FakeVault()
    with (
        patch("robothor.vault.set", new=vault.set),
        patch("robothor.vault.delete", new=vault.delete),
    ):
        yield vault


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """A throwaway manifest dir. ``_defaults.yaml`` is instance data (CLAUDE.md
    rule 11) and must never be read from — or written to — the real one."""
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setenv("ROBOTHOR_AGENTS_DIR", str(directory))
    import routers.providers as providers_module

    monkeypatch.setattr(providers_module, "_manifest_dir", lambda: directory)
    return directory


def _assert_no_secret(*payloads: object) -> None:
    for payload in payloads:
        assert FAKE_KEY not in json.dumps(payload, default=str)


class TestEngineClient:
    """The one function that actually dials the engine.

    Every test above patches it out, so without these the credential it mints
    is never exercised — and a token with the wrong audience or the wrong scope
    fails as a 403 at runtime, which is exactly the kind of thing a suite of
    green mocks will not tell you.
    """

    @pytest.fixture
    def _signing_key(self, monkeypatch):
        from robothor.auth import tokens

        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
        tokens.reset_signing_key_cache()
        yield
        tokens.reset_signing_key_cache()

    @pytest.mark.asyncio
    async def test_the_token_is_an_engine_control_credential(self, _signing_key) -> None:
        from routers import _engine_client

        from robothor.engine.auth import ENGINE_AUDIENCE, verify_engine_token

        context = verify_engine_token(_engine_client._engine_token())
        assert context.is_service
        assert context.has_scope("engine:control")
        assert ENGINE_AUDIENCE  # the audience the engine verifies against

    @pytest.mark.asyncio
    async def test_it_returns_the_status_and_decoded_body(self, _signing_key, monkeypatch):
        import httpx
        from routers import _engine_client

        seen = {}

        async def _handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"providers": []})

        # Captured before the patch: `_engine_client.httpx` IS the httpx
        # module, so a lambda that named httpx.AsyncClient would call itself.
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            _engine_client.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw),
        )
        status, body = await _engine_client.engine_request("GET", "/api/admin/providers")
        assert (status, body) == (200, {"providers": []})
        assert seen["url"].endswith("/api/admin/providers")
        assert seen["auth"].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_an_unreachable_engine_is_a_502_not_a_traceback(
        self, _signing_key, monkeypatch, caplog
    ):
        import httpx
        from routers import _engine_client

        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            _engine_client.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(_refuse), **kw),
        )
        with caplog.at_level(logging.DEBUG):
            status, body = await _engine_client.engine_request(
                "POST", "/api/admin/providers/openrouter/test", json={"api_key": FAKE_KEY}
            )
        assert status == 502
        assert body == {"error": "engine unavailable"}
        assert FAKE_KEY not in caplog.text, "a failure path must not log the request body"


class TestOperatorGate:
    def test_a_viewer_cannot_list_providers(self, controls_client_as_viewer) -> None:
        assert controls_client_as_viewer.get("/api/providers").status_code == 403

    def test_a_viewer_cannot_list_models(self, controls_client_as_viewer) -> None:
        assert controls_client_as_viewer.get("/api/models").status_code == 403

    def test_a_viewer_cannot_write_a_key(self, controls_client_as_viewer, fake_vault) -> None:
        response = controls_client_as_viewer.put(
            "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 403
        assert not fake_vault.written, "a rejected caller must not reach the vault"

    def test_a_service_token_cannot_write_a_key(
        self, controls_client_as_service, fake_vault
    ) -> None:
        response = controls_client_as_service.put(
            "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 403
        assert not fake_vault.written

    def test_a_viewer_cannot_run_a_test_connection(self, controls_client_as_viewer) -> None:
        response = controls_client_as_viewer.post(
            "/api/providers/openrouter/test", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 403

    def test_an_operator_can_list_providers(self, controls_client_as_operator, fake_engine):
        response = controls_client_as_operator.get("/api/providers")
        assert response.status_code == 200
        assert response.json() == ENGINE_PROVIDERS
        assert ("GET", "/api/admin/providers", None) in fake_engine.calls


class TestKeyWrites:
    def test_put_writes_the_vault_then_reloads_the_engine(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        response = controls_client_as_operator.put(
            "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 200
        assert fake_vault.written == {"providers/openrouter/api_key": FAKE_KEY}
        assert fake_engine.reload_count == 1

    def test_a_spare_slot_gets_the_numbered_key(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        controls_client_as_operator.put(
            "/api/providers/openrouter/keys/3", json={"api_key": FAKE_KEY}
        )
        assert "providers/openrouter/api_key_3" in fake_vault.written

    def test_the_response_is_a_fingerprint_and_never_the_key(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        body = controls_client_as_operator.put(
            "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
        ).json()
        assert body["configured"] is True
        assert body["position"] == 1
        assert body["fingerprint"].startswith("sha256:")
        _assert_no_secret(body)

    def test_an_empty_key_is_refused_before_the_vault_is_touched(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        response = controls_client_as_operator.put(
            "/api/providers/openrouter/keys/1", json={"api_key": "   "}
        )
        assert response.status_code == 422
        assert not fake_vault.written
        assert fake_engine.reload_count == 0

    def test_an_unknown_provider_is_refused(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        response = controls_client_as_operator.put(
            "/api/providers/not-a-provider/keys/1", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 404
        assert not fake_vault.written

    def test_a_position_below_one_is_refused(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        response = controls_client_as_operator.put(
            "/api/providers/openrouter/keys/0", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 422
        assert not fake_vault.written

    def test_a_slot_the_pool_would_never_read_is_refused(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        """The pool walks 16 slots. Storing a key at slot 40 would be a
        credential the UI calls configured and nothing ever dials."""
        from robothor.engine.key_pool import MAX_KEY_SLOTS

        response = controls_client_as_operator.put(
            f"/api/providers/openrouter/keys/{MAX_KEY_SLOTS + 1}", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 422
        assert not fake_vault.written

    def test_delete_removes_the_vault_row_and_reloads(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        fake_vault.written["providers/openrouter/api_key_2"] = FAKE_KEY
        response = controls_client_as_operator.delete("/api/providers/openrouter/keys/2")
        assert response.status_code == 200
        assert fake_vault.deleted == ["providers/openrouter/api_key_2"]
        assert fake_engine.reload_count == 1
        _assert_no_secret(response.json())

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/providers/openrouter/keys/1", {"nope": FAKE_KEY}),
            ("/api/providers/openrouter/keys/1", [FAKE_KEY]),
            ("/api/providers/openrouter/keys/1", {"api_key": 12345, "note": FAKE_KEY}),
        ],
    )
    def test_a_malformed_body_is_rejected_without_echoing_it(
        self, controls_client_as_operator, fake_engine, fake_vault, path, body
    ) -> None:
        """FastAPI's default 422 reflects the request body back in ``input``,
        and on this route the body IS the credential. A mistyped field name
        must not bounce the operator's key out of the appliance."""
        response = controls_client_as_operator.put(path, json=body)
        assert response.status_code == 422
        assert FAKE_KEY not in response.text
        assert not fake_vault.written

    def test_a_malformed_test_connection_body_is_not_echoed(
        self, controls_client_as_operator, fake_engine
    ) -> None:
        response = controls_client_as_operator.post(
            "/api/providers/openrouter/test", json=[FAKE_KEY]
        )
        assert response.status_code == 422
        assert FAKE_KEY not in response.text

    def test_validation_detail_elsewhere_is_untouched(self, controls_client_as_operator) -> None:
        """The redaction is scoped to the credential routes; a 422 on an
        ordinary route keeps the ``input`` that makes it debuggable."""
        response = controls_client_as_operator.post("/api/notes", json=["not-an-object"])
        assert response.status_code == 422
        assert "input" in response.text

    def test_the_key_never_reaches_a_log_line(
        self, controls_client_as_operator, fake_engine, fake_vault, caplog
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            controls_client_as_operator.put(
                "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
            )
        assert FAKE_KEY not in caplog.text

    def test_the_write_is_audited_by_identifier_only(
        self, controls_client_as_operator, fake_engine, fake_vault
    ) -> None:
        with patch("routers._audit.log_event") as log_event:
            controls_client_as_operator.put(
                "/api/providers/openrouter/keys/1", json={"api_key": FAKE_KEY}
            )
        assert log_event.called
        _assert_no_secret(log_event.call_args.kwargs)


class TestTestConnectionProxy:
    def test_it_proxies_the_body_to_the_engine(
        self, controls_client_as_operator, fake_engine
    ) -> None:
        response = controls_client_as_operator.post(
            "/api/providers/openrouter/test",
            json={"model": "openrouter/openai/gpt-5.4", "api_key": FAKE_KEY},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        method, path, body = fake_engine.calls[-1]
        assert (method, path) == ("POST", "/api/admin/providers/openrouter/test")
        assert body == {"model": "openrouter/openai/gpt-5.4", "api_key": FAKE_KEY}

    def test_it_never_logs_the_body(self, controls_client_as_operator, fake_engine, caplog) -> None:
        """The candidate key is in the request body of this route and nowhere
        else. A router that logs its own payload for debugging would put an
        unstored credential into the journal."""
        with caplog.at_level(logging.DEBUG):
            response = controls_client_as_operator.post(
                "/api/providers/openrouter/test", json={"api_key": FAKE_KEY}
            )
        assert FAKE_KEY not in caplog.text
        _assert_no_secret(response.json())

    def test_an_unknown_provider_is_refused_before_the_engine_is_called(
        self, controls_client_as_operator, fake_engine
    ) -> None:
        response = controls_client_as_operator.post(
            "/api/providers/nope/test", json={"api_key": FAKE_KEY}
        )
        assert response.status_code == 404
        assert fake_engine.calls == []


class TestModelsProxy:
    def test_it_returns_what_the_engine_knows(
        self, controls_client_as_operator, fake_engine
    ) -> None:
        response = controls_client_as_operator.get("/api/models")
        assert response.status_code == 200
        assert response.json() == ENGINE_MODELS


class TestDefaults:
    def _write_defaults(self, directory: Path, data: dict) -> Path:
        path = directory / "_defaults.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    def test_it_writes_the_model_block(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        path = self._write_defaults(
            agents_dir, {"model": {"primary": "old/model", "fallbacks": []}}
        )
        response = controls_client_as_operator.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": ["ollama_chat/qwen3:8b"]},
        )
        assert response.status_code == 200
        written = yaml.safe_load(path.read_text())
        assert written["model"]["primary"] == "openrouter/openai/gpt-5.4"
        assert written["model"]["fallbacks"] == ["ollama_chat/qwen3:8b"]

    def test_it_preserves_every_other_key(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        path = self._write_defaults(
            agents_dir,
            {
                "delivery": {"mode": "summary"},
                "model": {"primary": "old/model", "temperature": 0.4},
                "v2": {"sandbox": {"enabled": True}},
            },
        )
        controls_client_as_operator.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        written = yaml.safe_load(path.read_text())
        assert written["delivery"] == {"mode": "summary"}
        assert written["v2"] == {"sandbox": {"enabled": True}}
        assert written["model"]["temperature"] == 0.4, "an unrelated model key was dropped"

    def test_it_creates_the_file_when_there_is_none(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        response = controls_client_as_operator.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert response.status_code == 200
        assert (agents_dir / "_defaults.yaml").exists()

    def test_an_unknown_primary_model_is_refused(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        response = controls_client_as_operator.patch(
            "/api/providers/defaults", json={"model": "made/up", "fallbacks": []}
        )
        assert response.status_code == 422
        assert not (agents_dir / "_defaults.yaml").exists()

    def test_an_unknown_fallback_is_refused(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        response = controls_client_as_operator.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": ["made/up"]},
        )
        assert response.status_code == 422
        assert not (agents_dir / "_defaults.yaml").exists()

    def test_a_viewer_cannot_change_the_defaults(
        self, controls_client_as_viewer, agents_dir
    ) -> None:
        response = controls_client_as_viewer.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert response.status_code == 403
        assert not (agents_dir / "_defaults.yaml").exists()

    def test_the_write_leaves_no_temp_file_behind(
        self, controls_client_as_operator, fake_engine, agents_dir
    ) -> None:
        """Atomic means temp-then-rename; a crash between them must not leave
        a half-written manifest the engine would then fail to parse."""
        controls_client_as_operator.patch(
            "/api/providers/defaults",
            json={"model": "openrouter/openai/gpt-5.4", "fallbacks": []},
        )
        assert [p.name for p in agents_dir.iterdir()] == ["_defaults.yaml"]
