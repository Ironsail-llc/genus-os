"""The engine's provider/model admin surface.

Three properties are load-bearing here and each has its own defect history on
this instance:

1. **A credential never comes back out.** The bridge's ``/api/vault/get``
   answered an owner session with a decrypted secret for months. Every
   response this module produces is walked for the literal key string, not
   just eyeballed for a field name that looks safe.
2. **A test connection is a real completion.** A "test" that checks the key is
   non-empty is the inert-control pattern: green, and worth nothing. The route
   dials the provider and reports what came back, classified.
3. **A candidate key is used for one call and left nowhere.** The wizard tests
   a key before it is stored, so the route must reach litellm with it without
   writing it to ``os.environ`` (visible to every thread and every subprocess
   the engine spawns) or to the vault.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine import key_pool

ENV_KEY = "sk-env-000000000000000000000000"
VAULT_KEY = "sk-vault-11111111111111111111"
CANDIDATE_KEY = "sk-candidate-2222222222222222"


def _make_app():
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        return create_health_app(mock_config, runner=None, workflow_engine=None)


@pytest.fixture(autouse=True)
def _clean_provider_state(monkeypatch):
    key_pool.reset_shared_pools()
    key_pool._env_displaced.clear()
    for spec in key_pool.PROVIDERS:
        for index in range(1, 4):
            monkeypatch.delenv(
                spec.env_var if index == 1 else f"{spec.env_var}_{index}", raising=False
            )
    monkeypatch.setattr(key_pool, "_vault_read", lambda _key: None)
    monkeypatch.setattr(key_pool, "_vault_export", dict)
    key_pool.reset_vault_availability()
    yield
    key_pool.reset_shared_pools()
    key_pool._env_displaced.clear()


@pytest.fixture
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


def _vault(mapping: dict[str, str]):
    return lambda key: mapping.get(key)


def _assert_no_secret(payload: object, *secrets: str) -> None:
    """No key material anywhere in a response — field names are not a defence.

    Serialising the whole body and searching it is deliberate: a reviewer
    reading a handler can miss a credential that arrives inside an upstream
    error string, and that is exactly how keys have leaked here before.
    """
    rendered = json.dumps(payload, default=str)
    for secret in secrets:
        assert secret not in rendered, f"a credential leaked into a response: {secret[:6]}…"


class TestProviderListing:
    def test_it_lists_every_known_provider(self, client) -> None:
        body = client.get("/api/admin/providers").json()
        assert [p["id"] for p in body["providers"]] == [s.id for s in key_pool.PROVIDERS]
        for provider in body["providers"]:
            assert provider["label"]
            assert provider["env_var"]
            assert provider["default_model"]

    def test_an_unconfigured_provider_is_not_configured(self, client) -> None:
        body = client.get("/api/admin/providers").json()
        openai = next(p for p in body["providers"] if p["id"] == "openai")
        assert openai["configured"] is False
        assert openai["slots"] == []

    def test_env_only(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        body = client.get("/api/admin/providers").json()
        openrouter = next(p for p in body["providers"] if p["id"] == "openrouter")
        assert openrouter["configured"] is True
        assert [s["source"] for s in openrouter["slots"]] == ["env"]
        assert openrouter["slots"][0]["state"] == "active"
        assert openrouter["slots"][0]["updated_at"] is None
        _assert_no_secret(body, ENV_KEY)

    def test_vault_only(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        body = client.get("/api/admin/providers").json()
        anthropic = next(p for p in body["providers"] if p["id"] == "anthropic")
        assert anthropic["configured"] is True
        assert [s["source"] for s in anthropic["slots"]] == ["vault"]
        _assert_no_secret(body, VAULT_KEY)

    def test_mixed_slots(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/openrouter/api_key_2": VAULT_KEY})
        )
        body = client.get("/api/admin/providers").json()
        openrouter = next(p for p in body["providers"] if p["id"] == "openrouter")
        assert [(s["position"], s["source"], s["state"]) for s in openrouter["slots"]] == [
            (1, "env", "active"),
            (2, "vault", "spare"),
        ]
        _assert_no_secret(body, ENV_KEY, VAULT_KEY)

    def test_the_fingerprint_is_a_digest_and_not_the_key(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        body = client.get("/api/admin/providers").json()
        slot = next(p for p in body["providers"] if p["id"] == "openrouter")["slots"][0]
        assert slot["fingerprint"].startswith("sha256:")
        assert len(slot["fingerprint"]) == len("sha256:") + 8
        assert ENV_KEY[:8] not in slot["fingerprint"]

    def test_a_retired_key_is_reported_as_revoked(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        pool = key_pool.shared_pool("OPENROUTER_API_KEY")
        assert pool is not None
        pool.retire(ENV_KEY, key_pool.Retirement.AUTH_FAILED)
        body = client.get("/api/admin/providers").json()
        openrouter = next(p for p in body["providers"] if p["id"] == "openrouter")
        assert openrouter["slots"][0]["state"] == "revoked"


class TestModelListing:
    def test_it_lists_curated_registry_models(self, client) -> None:
        body = client.get("/api/admin/models").json()
        by_id = {m["id"]: m for m in body["models"]}
        assert "openrouter/anthropic/claude-sonnet-4.6" in by_id
        entry = by_id["openrouter/anthropic/claude-sonnet-4.6"]
        assert entry["source"] == "registry"
        assert entry["provider"] == "openrouter"
        assert entry["context_window"] > 0
        assert isinstance(entry["supports_thinking"], bool)
        assert "supports_tools" in entry

    def test_a_plugin_model_is_included_and_labelled(self, client) -> None:
        from robothor.engine import model_registry

        extra = {
            "acme/turbo": model_registry.ModelLimits(
                max_input_tokens=4096,
                max_output_tokens=1024,
                default_output_tokens=512,
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
            )
        }
        with patch.object(model_registry, "_plugin_model_limits", return_value=extra):
            body = client.get("/api/admin/models").json()
        by_id = {m["id"]: m for m in body["models"]}
        assert by_id["acme/turbo"]["source"] == "plugin"
        assert by_id["acme/turbo"]["context_window"] == 4096
        assert by_id["acme/turbo"]["provider"] == "acme"

    def test_a_broken_plugin_registry_does_not_break_the_listing(self, client) -> None:
        from robothor.engine import model_registry

        with patch.object(model_registry, "_plugin_model_limits", side_effect=RuntimeError("x")):
            response = client.get("/api/admin/models")
        assert response.status_code == 200
        assert response.json()["models"]


class TestConnection:
    @staticmethod
    def _ok_response():
        response = MagicMock()
        response.choices = [MagicMock()]
        return response

    def test_it_makes_one_real_completion_and_reports_ok(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        calls = []

        async def _fake(messages, **kwargs):
            calls.append((messages, kwargs))
            return self._ok_response()

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post("/api/admin/providers/openrouter/test", json={}).json()

        assert body["ok"] is True
        assert body["error_class"] is None
        assert body["model"] == "openrouter/openai/gpt-5.4"
        assert isinstance(body["latency_ms"], int)
        assert len(calls) == 1, "a test connection is exactly one completion"
        messages, kwargs = calls[0]
        assert messages == [{"role": "user", "content": "ping"}]
        assert kwargs["max_tokens"] == 1
        assert kwargs["timeout"] == 20

    def test_an_explicit_model_is_used(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)

        async def _fake(messages, **kwargs):
            return self._ok_response()

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post(
                "/api/admin/providers/openrouter/test",
                json={"model": "openrouter/z-ai/glm-5"},
            ).json()
        assert body["model"] == "openrouter/z-ai/glm-5"

    def test_a_candidate_key_is_passed_per_call_and_never_written_anywhere(
        self, client, monkeypatch
    ) -> None:
        seen = {}
        written = []

        async def _fake(messages, **kwargs):
            seen.update(kwargs)
            return self._ok_response()

        def _no_vault_write(*args, **kwargs):
            written.append(args)
            raise AssertionError("a test connection must never write to the vault")

        before = dict(os.environ)
        with (
            patch("robothor.engine.llm_client.llm_call", new=_fake),
            patch("robothor.vault.set", new=_no_vault_write),
        ):
            body = client.post(
                "/api/admin/providers/openrouter/test",
                json={"api_key": CANDIDATE_KEY},
            ).json()

        assert body["ok"] is True
        assert seen["api_key"] == CANDIDATE_KEY, "the candidate must reach litellm"
        assert os.environ == before, "a candidate key must not touch the process environment"
        assert CANDIDATE_KEY not in os.environ.values()
        assert not written
        _assert_no_secret(body, CANDIDATE_KEY)

    def test_the_configured_key_is_used_when_no_candidate_is_given(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        seen = {}

        async def _fake(messages, **kwargs):
            seen.update(kwargs)
            return self._ok_response()

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            client.post("/api/admin/providers/openrouter/test", json={})
        assert seen["api_key"] == ENV_KEY

    def test_an_unconfigured_provider_with_no_candidate_is_an_auth_failure(self, client) -> None:
        body = client.post("/api/admin/providers/openrouter/test", json={}).json()
        assert body["ok"] is False
        assert body["error_class"] == "auth"

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("AuthenticationError: 401 Unauthorized - invalid api key", "auth"),
            ("RateLimitError: 429 Too Many Requests", "rate_limit"),
            ("NotFoundError: 404 model 'nope' not found", "model_not_found"),
            ("Connection refused while dialling the provider", "network"),
            ("Something nobody has seen before", "unknown"),
        ],
    )
    def test_failures_are_classified(self, client, monkeypatch, message, expected) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)

        async def _fake(messages, **kwargs):
            raise RuntimeError(message)

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post("/api/admin/providers/openrouter/test", json={}).json()

        assert body["ok"] is False
        assert body["error_class"] == expected
        assert body["message"]

    def test_a_timeout_is_a_network_failure(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)

        async def _fake(messages, **kwargs):
            raise TimeoutError

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post("/api/admin/providers/openrouter/test", json={}).json()
        assert body["error_class"] == "network"
        assert body["ok"] is False

    def test_asyncio_timeout_is_the_same_exception_we_classify(self) -> None:
        """``llm_call`` raises ``asyncio.TimeoutError``; on 3.11+ that IS the
        builtin, so the case above covers it. Pinned rather than assumed."""
        assert asyncio.TimeoutError is TimeoutError

    def test_a_provider_error_that_echoes_the_key_is_redacted(self, client, monkeypatch) -> None:
        """Providers do echo the credential back in 401 bodies. This is the
        exact path an OpenRouter key took into a bench log on this instance."""
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)

        async def _fake(messages, **kwargs):
            raise RuntimeError(f"401 Unauthorized: no such key {ENV_KEY}")

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post(
                "/api/admin/providers/openrouter/test",
                json={"api_key": CANDIDATE_KEY},
            ).json()

        _assert_no_secret(body, ENV_KEY, CANDIDATE_KEY)
        assert "[redacted]" in body["message"]

    def test_an_unknown_provider_is_404(self, client) -> None:
        assert client.post("/api/admin/providers/nope/test", json={}).status_code == 404


class TestSecretsReload:
    def test_it_refreshes_the_environment_and_the_pool(self, client, monkeypatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {"PROVIDERS_OPENROUTER_API_KEY": VAULT_KEY},
        )
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/openrouter/api_key": VAULT_KEY})
        )

        body = client.post("/api/admin/secrets/reload").json()
        assert body == {"reloaded": ["openrouter"], "slots": 1}
        assert os.environ["OPENROUTER_API_KEY"] == VAULT_KEY

        pool = key_pool.shared_pool("OPENROUTER_API_KEY")
        assert pool is not None
        assert str(pool.current()) == VAULT_KEY
        _assert_no_secret(body, VAULT_KEY)

    def test_an_unreachable_vault_reloads_nothing(self, client, monkeypatch) -> None:
        def _explode() -> dict[str, str]:
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        response = client.post("/api/admin/secrets/reload")
        assert response.status_code == 200
        assert response.json() == {"reloaded": [], "slots": 0}


class TestSighup:
    def test_a_reload_signal_also_refreshes_provider_secrets(self, monkeypatch) -> None:
        """SIGHUP already means "re-read what is on disk" for plugins. A
        credential written to the vault by another process — the bridge, the
        CLI — is the same kind of change, and an operator who has just run
        ``genus vault set`` should not have to restart the engine."""
        from robothor.engine import daemon

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {"PROVIDERS_OPENROUTER_API_KEY": VAULT_KEY},
        )
        monkeypatch.setattr(daemon, "reload_plugins", lambda: 7)
        monkeypatch.setattr(daemon, "_ACTIVE_SCHEDULER", None)

        assert daemon._handle_plugin_reload_signal() == 7
        assert os.environ["OPENROUTER_API_KEY"] == VAULT_KEY

    def test_a_failing_secrets_reload_never_kills_the_daemon(self, monkeypatch) -> None:
        from robothor.engine import daemon

        monkeypatch.setattr(daemon, "reload_plugins", lambda: 3)
        monkeypatch.setattr(daemon, "_ACTIVE_SCHEDULER", None)
        monkeypatch.setattr(
            key_pool,
            "reload_provider_keys",
            MagicMock(side_effect=RuntimeError("vault on fire")),
        )
        assert daemon._handle_plugin_reload_signal() == 3


class TestScope:
    def test_admin_routes_demand_engine_control(self) -> None:
        """``/api/admin`` is the whole credential surface; a read-scoped
        dashboard token must not be able to enumerate or dial it."""
        from robothor.engine.auth import required_scope

        for method, path in [
            ("GET", "/api/admin/providers"),
            ("GET", "/api/admin/models"),
            ("POST", "/api/admin/providers/openrouter/test"),
            ("POST", "/api/admin/secrets/reload"),
        ]:
            assert required_scope(method, path) == "engine:control", path
