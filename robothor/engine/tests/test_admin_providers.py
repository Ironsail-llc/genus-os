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

from robothor.engine import admin_providers, key_pool

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
    monkeypatch.setattr(key_pool, "_vault_export", dict)
    monkeypatch.setattr(admin_providers, "_timestamps_for_vault_slots", dict)
    key_pool.reset_vault_availability()
    yield
    key_pool.reset_shared_pools()
    key_pool._env_displaced.clear()


#: The real vault-timestamp reader, captured before the autouse fixture stubs
#: the module attribute, so a test can put it back by identity.
_real_timestamps = admin_providers._timestamps_for_vault_slots


@pytest.fixture
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


def _vault(mapping: dict[str, str]):
    """A vault holding these keys, as ``export_env`` would render it."""
    from robothor.vault.naming import env_name

    exported = {env_name(k): v for k, v in mapping.items()}
    return lambda: dict(exported)


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
            key_pool, "_vault_export", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        body = client.get("/api/admin/providers").json()
        anthropic = next(p for p in body["providers"] if p["id"] == "anthropic")
        assert anthropic["configured"] is True
        assert [s["source"] for s in anthropic["slots"]] == ["vault"]
        _assert_no_secret(body, VAULT_KEY)

    def test_mixed_slots(self, client, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key_2": VAULT_KEY})
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

    @pytest.mark.parametrize(
        "body",
        [
            [CANDIDATE_KEY],
            {"api_key": 12345, "note": CANDIDATE_KEY},
            {"model": {"nested": CANDIDATE_KEY}},
        ],
    )
    def test_a_malformed_body_is_rejected_without_echoing_it(self, client, body) -> None:
        """FastAPI's default 422 reflects the request body back in ``input``.

        On every other route that is a convenience. On this one the body IS a
        credential, so a typo'd field name would bounce the operator's key
        straight back out — and into anything that records 4xx bodies.
        """
        response = client.post("/api/admin/providers/openrouter/test", json=body)
        assert response.status_code == 422
        assert CANDIDATE_KEY not in response.text

    def test_other_routes_keep_their_ordinary_validation_detail(self, client) -> None:
        """The redaction is scoped. A generic 422 elsewhere stays useful."""
        response = client.post("/api/admin/providers/openrouter/test", json=[CANDIDATE_KEY])
        assert "detail" in response.json()

    def test_an_unknown_provider_is_404(self, client) -> None:
        assert client.post("/api/admin/providers/nope/test", json={}).status_code == 404


class TestSecretsReload:
    def test_it_refreshes_the_environment_and_the_pool(self, client, monkeypatch) -> None:
        # setenv, not delenv: monkeypatch has to have SEEN the variable to undo
        # what reload_provider_keys writes to it directly, or the value leaks
        # into every test that runs after this one.
        monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-overwritten-by-the-reload")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {"PROVIDERS_OPENROUTER_API_KEY": VAULT_KEY},
        )
        monkeypatch.setattr(
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key": VAULT_KEY})
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


class TestCredentialPathCoverage:
    def test_every_admin_provider_route_is_treated_as_credential_bearing(self) -> None:
        """The redaction is keyed on path prefixes, so a renamed route would
        silently stop being protected. Checked against the app's own routes
        rather than against a second list of the same strings."""
        from robothor.credential_errors import carries_credentials

        app = _make_app()
        try:
            # FastAPI >= 0.139 keeps included routers as lazy groups, so a
            # naive walk of app.routes sees none of them.
            from fastapi.routing import iter_route_contexts

            routes = list(iter_route_contexts(app.routes))
        except ImportError:  # pragma: no cover - FastAPI < 0.139
            routes = list(app.routes)
        paths = {
            route.path for route in routes if getattr(route, "path", "").startswith("/api/admin")
        }
        assert paths, "route enumeration collapsed — the assertion below would be vacuous"
        uncovered = [path for path in paths if not carries_credentials(path)]
        assert not uncovered, f"unprotected credential routes: {sorted(uncovered)}"


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


class TestRequestModelRedaction:
    """The request model is a frame local, and frame locals get printed.

    structlog's console renderer formats exceptions with
    ``show_locals=True``. A plain ``str`` field would put the key the operator
    has just typed — and not yet stored anywhere — into the journal the first
    time anything in the handler raised.
    """

    def test_repr_and_str_hide_the_key(self) -> None:
        model = admin_providers.TestConnectionRequest(api_key=CANDIDATE_KEY, model="m")
        assert CANDIDATE_KEY not in repr(model)
        assert CANDIDATE_KEY not in str(model)
        assert CANDIDATE_KEY not in repr(model.api_key)

    def test_the_value_is_still_reachable_for_the_one_call(self) -> None:
        model = admin_providers.TestConnectionRequest(api_key=CANDIDATE_KEY)
        assert model.api_key is not None
        assert model.api_key.get_secret_value() == CANDIDATE_KEY

    def test_a_dumped_model_does_not_carry_the_key(self) -> None:
        model = admin_providers.TestConnectionRequest(api_key=CANDIDATE_KEY)
        assert CANDIDATE_KEY not in str(model.model_dump())


class TestVaultIoIsOffTheEventLoop:
    def test_the_listing_awaits_a_thread(self, client, monkeypatch) -> None:
        """psycopg2 is synchronous. A status page that blocks the event loop
        stalls every agent turn and every webhook in flight."""
        threads: list[str] = []
        real_to_thread = asyncio.to_thread

        async def _record(func, /, *args, **kwargs):
            threads.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        # The autouse fixture stubs the vault read out; put the real one back
        # so this test observes the call the route actually makes.
        monkeypatch.setattr(
            admin_providers,
            "_timestamps_for_vault_slots",
            _real_timestamps,
        )
        monkeypatch.setattr(admin_providers.asyncio, "to_thread", _record)
        assert client.get("/api/admin/providers").status_code == 200
        assert "_timestamps_for_vault_slots" in threads

    def test_the_reload_awaits_a_thread(self, client, monkeypatch) -> None:
        threads: list[str] = []
        real_to_thread = asyncio.to_thread

        async def _record(func, /, *args, **kwargs):
            threads.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(admin_providers.asyncio, "to_thread", _record)
        assert client.post("/api/admin/secrets/reload").status_code == 200
        assert "reload_provider_keys" in threads

    def test_the_listing_reads_the_vault_once_for_every_provider(self, client, monkeypatch) -> None:
        calls: list[int] = []

        def _export() -> dict[str, str]:
            calls.append(1)
            return {}

        monkeypatch.setattr(key_pool, "_vault_export", _export)
        monkeypatch.setattr(admin_providers, "_timestamps_for_vault_slots", _real_timestamps)
        assert client.get("/api/admin/providers").status_code == 200
        assert len(calls) == 1, f"{len(calls)} vault reads for one listing"


class TestStorageIndexInTheListing:
    def test_a_stranded_slot_keeps_its_real_number_and_says_it_is_orphaned(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            _vault(
                {
                    "providers/openrouter/api_key": VAULT_KEY,
                    "providers/openrouter/api_key_3": "sk-vault-spare-3333333333",
                }
            ),
        )
        body = client.get("/api/admin/providers").json()
        openrouter = next(p for p in body["providers"] if p["id"] == "openrouter")
        assert [(s["position"], s["state"]) for s in openrouter["slots"]] == [
            (1, "active"),
            (3, "orphaned"),
        ]
        _assert_no_secret(body, ENV_KEY, VAULT_KEY, "sk-vault-spare-3333333333")


class TestTestModelValidation:
    def test_a_model_from_another_provider_is_refused(self, client, monkeypatch) -> None:
        """The only genuinely security-shaped check here: one provider's key
        must not be posted to another provider's endpoint."""
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        called = []

        async def _fake(messages, **kwargs):
            called.append(1)

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            response = client.post(
                "/api/admin/providers/openrouter/test", json={"model": "gemini/gemini-2.5-flash"}
            )
        assert response.status_code == 422
        assert not called

    def test_an_unknown_model_is_refused_before_the_provider_is_dialled(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        called = []

        async def _fake(messages, **kwargs):
            called.append(1)

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            response = client.post(
                "/api/admin/providers/openrouter/test", json={"model": "openrouter/made/up"}
            )
        assert response.status_code == 422
        assert not called

    def test_the_provider_default_is_always_acceptable(self, client, monkeypatch) -> None:
        """Three of five defaults are not registry entries. Rejecting the model
        the wizard's own button sends would be worse than not validating."""
        monkeypatch.setattr(
            key_pool, "_vault_export", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        spec = key_pool.provider_by_id("anthropic")
        assert spec is not None

        async def _fake(messages, **kwargs):
            response = MagicMock()
            response.choices = [MagicMock()]
            return response

        with patch("robothor.engine.llm_client.llm_call", new=_fake):
            body = client.post(
                "/api/admin/providers/anthropic/test", json={"model": spec.default_model}
            ).json()
        assert body["ok"] is True


class TestDefaultsReload:
    def test_it_reports_the_model_block_now_in_effect(self, client, tmp_path, monkeypatch) -> None:
        """Manifests stay the source of truth, so a UI that rewrites the YAML
        has to be able to tell the engine — and be told what took effect."""
        import yaml

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "_defaults.yaml").write_text(
            yaml.safe_dump({"model": {"primary": "openrouter/z-ai/glm-5", "fallbacks": ["x/y"]}})
        )
        monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)

        body = client.post("/api/admin/defaults/reload").json()
        assert body == {
            "reloaded": True,
            "primary": "openrouter/z-ai/glm-5",
            "fallbacks": ["x/y"],
        }

    def test_it_drops_the_cache_so_a_rewrite_is_seen(self, client, tmp_path, monkeypatch) -> None:
        import yaml

        from robothor.engine import config as engine_config

        agents = tmp_path / "agents"
        agents.mkdir()
        path = agents / "_defaults.yaml"
        path.write_text(yaml.safe_dump({"model": {"primary": "first/model"}}))
        monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)

        client.post("/api/admin/defaults/reload")
        # Same mtime, different content: without an explicit cache drop the
        # engine would keep serving the block it parsed a moment ago.
        stat = path.stat()
        path.write_text(yaml.safe_dump({"model": {"primary": "second/model"}}))
        os.utime(path, (stat.st_atime, stat.st_mtime))

        body = client.post("/api/admin/defaults/reload").json()
        assert body["primary"] == "second/model"
        assert engine_config._load_defaults(agents)["model"]["primary"] == "second/model"

    def test_a_missing_defaults_file_is_not_an_error(self, client, tmp_path, monkeypatch) -> None:
        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(admin_providers, "_manifest_dir", lambda: agents)
        body = client.post("/api/admin/defaults/reload").json()
        assert body == {"reloaded": True, "primary": None, "fallbacks": []}


class TestStartupSecretsLoad:
    @pytest.mark.asyncio
    async def test_the_daemon_loads_vault_keys_before_subsystems_run(self, monkeypatch) -> None:
        """A vault-only box was dark for every direct-env consumer — memory
        generation reads OPENROUTER_API_KEY itself — until someone sent SIGHUP."""
        from robothor.engine import daemon

        monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key": VAULT_KEY})
        )

        assert await daemon.load_provider_secrets_at_startup() == 1
        assert os.environ["OPENROUTER_API_KEY"] == VAULT_KEY

    @pytest.mark.asyncio
    async def test_a_box_with_no_vault_still_starts(self, monkeypatch) -> None:
        from robothor.engine import daemon

        def _explode() -> dict[str, str]:
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        assert await daemon.load_provider_secrets_at_startup() == 0

    @pytest.mark.asyncio
    async def test_a_broken_key_pool_never_blocks_boot(self, monkeypatch) -> None:
        from robothor.engine import daemon
        from robothor.engine import key_pool as kp

        monkeypatch.setattr(kp, "reload_provider_keys", MagicMock(side_effect=RuntimeError("boom")))
        assert await daemon.load_provider_secrets_at_startup() == 0

    @pytest.mark.asyncio
    async def test_it_runs_off_the_event_loop(self, monkeypatch) -> None:
        from robothor.engine import daemon

        threads: list[str] = []
        real_to_thread = asyncio.to_thread

        async def _record(func, /, *args, **kwargs):
            threads.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(daemon.asyncio, "to_thread", _record)
        await daemon.load_provider_secrets_at_startup()
        assert "reload_provider_keys" in threads
