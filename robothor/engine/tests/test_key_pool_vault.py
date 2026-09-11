"""Provider credentials resolve vault-first, then environment.

Until now a key could only arrive through the environment, which means
through a file an operator edits over ssh. The UI writes to the vault
instead, so the pool has to read both — and the order matters: the vault is
what the operator just changed in the browser, the environment is what the
box was started with. A vault write that loses to a stale env var is the
same bug as no write at all.

The third case is the one that takes an appliance down: on a box with no
vault master key (a fresh install, a tmpfs that has not been populated yet)
``vault.get`` raises. If that reaches ``shared_pool`` the engine cannot make
an LLM call at all — strictly worse than the env-only behaviour it replaced.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import key_pool

ENV_KEY = "env-key-aaaaaaaaaaaaaaaaaaaaaaaa"
ENV_SPARE = "env-spare-bbbbbbbbbbbbbbbbbbbb"
VAULT_KEY = "vault-key-cccccccccccccccccccc"
VAULT_SPARE = "vault-spare-dddddddddddddddddd"


@pytest.fixture(autouse=True)
def _clean_pools(monkeypatch):
    """No cached pool and no inherited provider credential leaks into a case."""
    key_pool.reset_shared_pools()
    for spec in key_pool.PROVIDERS:
        for index in range(1, 4):
            name = spec.env_var if index == 1 else f"{spec.env_var}_{index}"
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(key_pool, "_vault_read", lambda _key: None)
    key_pool.reset_vault_availability()
    # Process-global by design (a reload has to know what it displaced across
    # calls), so a case must not inherit the previous case's bookkeeping.
    key_pool._env_displaced.clear()
    yield
    key_pool.reset_shared_pools()
    key_pool._env_displaced.clear()


def _vault(mapping: dict[str, str]):
    return lambda key: mapping.get(key)


class TestProviderCatalog:
    def test_the_four_new_providers_are_registered(self) -> None:
        by_id = {spec.id: spec for spec in key_pool.PROVIDERS}
        assert by_id["anthropic"].env_var == "ANTHROPIC_API_KEY"
        assert by_id["openai"].env_var == "OPENAI_API_KEY"
        assert by_id["gemini"].env_var == "GEMINI_API_KEY"
        assert by_id["deepseek"].env_var == "DEEPSEEK_API_KEY"

    def test_openrouter_keeps_its_existing_semantics(self) -> None:
        assert key_pool._PROVIDER_KEY_VARS["openrouter/"] == "OPENROUTER_API_KEY"
        assert key_pool.env_var_for_model("openrouter/anthropic/claude-x") == "OPENROUTER_API_KEY"

    def test_an_unpooled_model_still_gets_no_pool(self) -> None:
        assert key_pool.env_var_for_model("ollama_chat/qwen3:8b") is None

    def test_every_provider_prefix_maps_back_to_its_var(self) -> None:
        for spec in key_pool.PROVIDERS:
            assert key_pool._PROVIDER_KEY_VARS[spec.model_prefix] == spec.env_var


class TestResolutionOrder:
    def test_vault_wins_over_env_for_the_same_slot(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/openrouter/api_key": VAULT_KEY})
        )
        slots = key_pool.resolve_keys("openrouter")
        assert [(s.position, s.source, s.key) for s in slots] == [(1, "vault", VAULT_KEY)]

    def test_env_is_used_when_the_vault_has_nothing(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        slots = key_pool.resolve_keys("openrouter")
        assert [(s.position, s.source, s.key) for s in slots] == [(1, "env", ENV_KEY)]

    def test_slots_may_be_mixed_across_sources(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_SPARE)
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/openrouter/api_key_2": VAULT_SPARE})
        )
        slots = key_pool.resolve_keys("openrouter")
        assert [(s.position, s.source) for s in slots] == [(1, "env"), (2, "vault")]
        assert slots[1].key == VAULT_SPARE

    def test_resolution_stops_at_the_first_empty_slot(self, monkeypatch) -> None:
        """Same rule ``keys_from_env`` already applies: a hole is a typo."""
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_3", ENV_SPARE)
        assert [s.position for s in key_pool.resolve_keys("openrouter")] == [1]

    def test_a_duplicate_value_does_not_take_a_second_slot(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_KEY)
        assert len(key_pool.resolve_keys("openrouter")) == 1

    def test_an_unconfigured_provider_resolves_to_nothing(self) -> None:
        assert key_pool.resolve_keys("deepseek") == []

    def test_an_unknown_provider_id_resolves_to_nothing(self) -> None:
        assert key_pool.resolve_keys("not-a-provider") == []

    def test_the_shared_pool_is_built_from_the_resolved_slots(self, monkeypatch) -> None:
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        pool = key_pool.shared_pool("ANTHROPIC_API_KEY")
        assert pool is not None
        assert str(pool.current()) == VAULT_KEY

    def test_api_key_for_model_reaches_a_vault_only_provider(self, monkeypatch) -> None:
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        assert key_pool.api_key_for_model("anthropic/claude-sonnet-4.6") == VAULT_KEY


class TestUninitialisedVault:
    def test_it_degrades_to_env_without_raising(self, monkeypatch) -> None:
        def _explode(_key: str) -> str | None:
            raise FileNotFoundError("Vault master key not found at /nope/.vault-key")

        monkeypatch.setattr(key_pool, "_vault_read", _explode)
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        key_pool.reset_vault_availability()

        slots = key_pool.resolve_keys("openrouter")
        assert [(s.position, s.source, s.key) for s in slots] == [(1, "env", ENV_KEY)]

    def test_it_says_so_once_at_info_and_not_again(self, monkeypatch, caplog) -> None:
        def _explode(_key: str) -> str | None:
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_read", _explode)
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        key_pool.reset_vault_availability()

        with caplog.at_level(logging.INFO, logger="robothor.engine.key_pool"):
            for _ in range(3):
                key_pool.resolve_keys("openrouter")
                key_pool.resolve_keys("anthropic")

        vault_lines = [r for r in caplog.records if "vault" in r.message.lower()]
        assert len(vault_lines) == 1, f"expected one INFO line, got {vault_lines}"
        assert vault_lines[0].levelno == logging.INFO

    def test_the_module_does_not_import_the_vault_at_import_time(self) -> None:
        """A vault import at module scope drags psycopg2 + the master key into
        every engine import, including the ones that run before secrets are on
        disk."""
        import ast
        import pathlib

        source = pathlib.Path(key_pool.__file__).read_text()
        tree = ast.parse(source)
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if "vault" in name
        ]
        assert not top_level_imports, "robothor.vault must be imported lazily"


class TestSlotReporting:
    def test_slots_carry_a_fingerprint_and_never_the_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool, "_vault_read", _vault({"providers/openrouter/api_key_2": VAULT_SPARE})
        )
        slots = key_pool.provider_slots("openrouter")
        assert [s.position for s in slots] == [1, 2]
        assert [s.source for s in slots] == ["env", "vault"]
        for slot in slots:
            assert slot.fingerprint.startswith("sha256:")
            assert len(slot.fingerprint) == len("sha256:") + 8
        rendered = repr(slots)
        assert ENV_KEY not in rendered
        assert VAULT_SPARE not in rendered

    def test_fingerprints_differ_per_key_and_are_stable(self) -> None:
        assert key_pool.key_fingerprint(ENV_KEY) == key_pool.key_fingerprint(ENV_KEY)
        assert key_pool.key_fingerprint(ENV_KEY) != key_pool.key_fingerprint(VAULT_KEY)

    def test_the_first_available_slot_is_active_and_the_rest_are_spare(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_SPARE)
        states = [s.state for s in key_pool.provider_slots("openrouter")]
        assert states == ["active", "spare"]

    def test_a_retired_key_reports_why(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_SPARE)
        pool = key_pool.shared_pool("OPENROUTER_API_KEY")
        assert pool is not None
        pool.retire(ENV_KEY, key_pool.Retirement.AUTH_FAILED)
        pool.retire(ENV_SPARE, key_pool.Retirement.CREDIT_EXHAUSTED)
        assert [s.state for s in key_pool.provider_slots("openrouter")] == ["revoked", "capped"]

    def test_an_unconfigured_provider_has_no_slots(self) -> None:
        assert key_pool.provider_slots("openai") == []


class TestReload:
    def test_it_refreshes_the_environment_from_the_vault(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {"PROVIDERS_OPENROUTER_API_KEY": VAULT_KEY},
        )
        result = key_pool.reload_provider_keys()
        import os

        assert os.environ["OPENROUTER_API_KEY"] == VAULT_KEY
        assert result.reloaded == ["openrouter"]
        assert result.slots == 1

    def test_it_restores_the_prior_environment_value_when_the_vault_row_goes(
        self, monkeypatch
    ) -> None:
        import os

        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {"PROVIDERS_OPENROUTER_API_KEY": VAULT_KEY},
        )
        key_pool.reload_provider_keys()
        assert os.environ["OPENROUTER_API_KEY"] == VAULT_KEY

        monkeypatch.setattr(key_pool, "_vault_export", dict)
        key_pool.reload_provider_keys()
        assert os.environ["OPENROUTER_API_KEY"] == ENV_KEY

    def test_it_drops_the_cached_pools(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        first = key_pool.shared_pool("OPENROUTER_API_KEY")
        monkeypatch.setattr(key_pool, "_vault_export", dict)
        key_pool.reload_provider_keys()
        assert key_pool.shared_pool("OPENROUTER_API_KEY") is not first

    def test_an_unreachable_vault_reloads_nothing_and_does_not_raise(self, monkeypatch) -> None:
        def _explode() -> dict[str, str]:
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        result = key_pool.reload_provider_keys()
        assert result.reloaded == []
        assert result.slots == 0
