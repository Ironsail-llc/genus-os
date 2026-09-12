"""One provider, one credential — resolved AFTER the provider is chosen.

The defect this module exists to prevent: the wizard used to take the first key
the environment carried, in ``key_pool.PROVIDERS`` order, without looking at
which provider the operator picked. On a box with both an OpenRouter and an
Anthropic key — the normal state of a working instance — ``--provider
anthropic`` put a live OpenRouter key in an ``Authorization`` header to
``api.anthropic.com``. The disclosure went to a vendor that must never see it,
and the symptom was a 401 that read as "your Anthropic key is bad", so the
natural next action was to paste more credentials.

So: the candidate credential comes from ``key_pool.scan_slots(provider_id)``,
which is vault-first and then that provider's own environment variable, and a
provider with no credential gets ``api_key=None`` rather than someone else's.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.init.context import InitContext
from robothor.init.provider_probe import ProbeResult, resolve_provider_key
from robothor.init.steps import ProviderStep

# Obvious stand-ins. Neither is a real credential shape for either vendor.
OPENROUTER_KEY = "test-openrouter-credential"
ANTHROPIC_KEY = "test-anthropic-credential"


@pytest.fixture
def _two_vendors(monkeypatch):
    """A box carrying two vendors' keys, each in its own provider's slot 1."""
    from robothor.engine.key_pool import ResolvedKey

    keys = {"openrouter": OPENROUTER_KEY, "anthropic": ANTHROPIC_KEY}

    def fake_scan_slots(provider_id: str) -> list[ResolvedKey]:
        value = keys.get(provider_id)
        if not value:
            return []
        return [ResolvedKey(position=1, key=value, source="env")]

    monkeypatch.setattr("robothor.engine.key_pool.scan_slots", fake_scan_slots)
    return keys


def _ctx(tmp_path, **kwargs: Any) -> InitContext:
    kwargs.setdefault("yes", True)
    return InitContext(workspace=tmp_path / "workspace", **kwargs)


class TestResolveProviderKey:
    def test_it_returns_that_provider_s_own_credential(self, _two_vendors):
        assert resolve_provider_key("anthropic") == ANTHROPIC_KEY
        assert resolve_provider_key("openrouter") == OPENROUTER_KEY

    def test_a_provider_with_no_credential_gets_none_not_someone_else_s(self, _two_vendors):
        assert resolve_provider_key("gemini") is None

    def test_an_unknown_provider_is_none_rather_than_an_exception(self, _two_vendors):
        assert resolve_provider_key("ollama") is None

    def test_an_unreadable_vault_is_none_not_a_crash(self, monkeypatch):
        def boom(provider_id: str) -> list[Any]:
            raise RuntimeError("no database, so no vault")

        monkeypatch.setattr("robothor.engine.key_pool.scan_slots", boom)

        assert resolve_provider_key("openrouter") is None


class TestTheProbeGetsTheChosenVendorsKey:
    def test_the_chosen_provider_s_key_is_the_one_dialled(self, tmp_path, _two_vendors):
        seen: dict[str, Any] = {}

        def probe(model: str, *, api_key: str | None = None, **_: Any) -> ProbeResult:
            seen["model"] = model
            seen["api_key"] = api_key
            return ProbeResult(True, "anthropic", model, "ok", True)

        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "anthropic", "provider_model": "anthropic/claude-sonnet-4.6"},
        )
        ProviderStep(probe=probe).apply(ctx)

        assert seen["api_key"] == ANTHROPIC_KEY
        assert seen["api_key"] != OPENROUTER_KEY

    def test_a_provider_without_a_credential_dials_with_none(self, tmp_path, _two_vendors):
        seen: dict[str, Any] = {}

        def probe(model: str, *, api_key: str | None = None, **_: Any) -> ProbeResult:
            seen["api_key"] = api_key
            return ProbeResult(True, "gemini", model, "ok", True)

        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "gemini", "provider_model": "gemini/gemini-2.5-flash"},
        )
        ProviderStep(probe=probe).apply(ctx)

        assert seen["api_key"] is None

    def test_a_local_ollama_model_is_never_handed_a_cloud_key(self, tmp_path, _two_vendors):
        seen: dict[str, Any] = {}

        def probe(model: str, *, api_key: str | None = None, **_: Any) -> ProbeResult:
            seen["api_key"] = api_key
            return ProbeResult(True, "", model, "ok", True)

        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "ollama", "provider_model": "ollama/qwen3:8b"},
        )
        ProviderStep(probe=probe).apply(ctx)

        assert seen["api_key"] is None

    def test_no_answer_carries_key_material(self, tmp_path, _two_vendors):
        """A credential in ``ctx.answers`` would reach the plan, the JSON and
        the state file the moment anyone rendered answers for debugging."""

        def probe(model: str, *, api_key: str | None = None, **_: Any) -> ProbeResult:
            return ProbeResult(True, "anthropic", model, "ok", True)

        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "anthropic", "provider_model": "anthropic/claude-sonnet-4.6"},
        )
        ProviderStep(probe=probe).apply(ctx)

        rendered = repr(ctx.answers) + repr(ctx.details)
        assert ANTHROPIC_KEY not in rendered
        assert OPENROUTER_KEY not in rendered


class TestAReRunIsNotBlocked:
    def test_a_vault_stored_key_satisfies_the_check(self, tmp_path, monkeypatch):
        """After an install the credential lives in the vault, not the shell.
        A phase-1 refusal there would block the routine re-run the deployment
        docs promise."""
        from robothor.engine.key_pool import ResolvedKey

        def vault_only(provider_id: str) -> list[ResolvedKey]:
            if provider_id != "openrouter":
                return []
            return [ResolvedKey(position=1, key=OPENROUTER_KEY, source="vault")]

        monkeypatch.setattr("robothor.engine.key_pool.scan_slots", vault_only)

        ctx = _ctx(tmp_path)
        result = ProviderStep().check(ctx)

        assert result.ok is True
        assert "openrouter" in result.detail

    def test_a_model_already_recorded_reads_as_exists(self, tmp_path, monkeypatch):
        from robothor.engine.key_pool import ResolvedKey

        monkeypatch.setattr(
            "robothor.engine.key_pool.scan_slots",
            lambda provider_id: (
                [ResolvedKey(position=1, key=OPENROUTER_KEY, source="vault")]
                if provider_id == "openrouter"
                else []
            ),
        )

        class _Providers:
            last_resort_model = "openrouter/openai/gpt-5.4"

        class _Settings:
            providers = _Providers()

        ctx = _ctx(tmp_path, settings_factory=_Settings)
        result = ProviderStep().check(ctx)

        assert result.action == "exists"
        assert "openrouter/openai/gpt-5.4" in result.detail

    def test_no_credential_anywhere_still_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("robothor.engine.key_pool.scan_slots", lambda provider_id: [])

        result = ProviderStep().check(_ctx(tmp_path))

        assert result.ok is False
        assert "OPENROUTER_API_KEY" in result.fix_hint

    def test_an_unmatched_provider_says_so_instead_of_naming_an_empty_model(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("robothor.engine.key_pool.scan_slots", lambda provider_id: [])

        ctx = _ctx(tmp_path, answers={"provider_id": "gemini"})
        result = ProviderStep().check(ctx)

        assert result.ok is False
        assert "gemini" in result.detail
        assert "will test  with" not in result.detail
