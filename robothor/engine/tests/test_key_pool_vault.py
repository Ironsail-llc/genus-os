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
    monkeypatch.setattr(key_pool, "_vault_export", dict)
    key_pool.reset_vault_availability()
    # Process-global by design (a reload has to know what it displaced across
    # calls), so a case must not inherit the previous case's bookkeeping.
    key_pool._env_displaced.clear()
    yield
    key_pool.reset_shared_pools()
    key_pool._env_displaced.clear()


def _vault(mapping: dict[str, str]):
    """A vault holding these keys, as ``export_env`` would render it."""
    from robothor.vault.naming import env_name

    exported = {env_name(k): v for k, v in mapping.items()}
    return lambda: dict(exported)


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
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key": VAULT_KEY})
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
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key_2": VAULT_SPARE})
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
            key_pool, "_vault_export", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        pool = key_pool.shared_pool("ANTHROPIC_API_KEY")
        assert pool is not None
        assert str(pool.current()) == VAULT_KEY

    def test_api_key_for_model_reaches_a_vault_only_provider(self, monkeypatch) -> None:
        monkeypatch.setattr(
            key_pool, "_vault_export", _vault({"providers/anthropic/api_key": VAULT_KEY})
        )
        assert key_pool.api_key_for_model("anthropic/claude-sonnet-4.6") == VAULT_KEY


class TestUninitialisedVault:
    def test_it_degrades_to_env_without_raising(self, monkeypatch) -> None:
        def _explode() -> dict[str, str]:
            raise FileNotFoundError("Vault master key not found at /nope/.vault-key")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        key_pool.reset_vault_availability()

        slots = key_pool.resolve_keys("openrouter")
        assert [(s.position, s.source, s.key) for s in slots] == [(1, "env", ENV_KEY)]

    def test_it_says_so_once_and_not_again(self, monkeypatch, caplog) -> None:
        def _explode() -> dict[str, str]:
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        key_pool.reset_vault_availability()

        with caplog.at_level(logging.INFO, logger="robothor.engine.key_pool"):
            for _ in range(3):
                key_pool.resolve_keys("openrouter")
                key_pool.resolve_keys("anthropic")

        vault_lines = [r for r in caplog.records if "vault" in r.message.lower()]
        assert len(vault_lines) == 1, f"expected one line, got {vault_lines}"
        # WARNING, not INFO: on a box that has a vault, an unreadable one is a
        # real outage of the credential store and must not need the log level
        # raised to be seen. The retry-after is what keeps it to one line.
        assert vault_lines[0].levelno == logging.WARNING

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
            key_pool, "_vault_export", _vault({"providers/openrouter/api_key_2": VAULT_SPARE})
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


class TestStorageIndex:
    """A slot number names a vault row, not a place in a list.

    The two were the same number until a gap or a duplicate made them differ,
    and a dense display index then addressed the wrong row: the listing named
    a slot that did not exist, ``updated_at`` looked up a key nobody wrote, and
    ``DELETE …/keys/2`` deleted nothing while reporting success.
    """

    @pytest.fixture
    def gap_and_duplicate(self, monkeypatch):
        """Vault slots 1 and 3, plus an env value for slot 1 that loses to it."""
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            _vault(
                {
                    "providers/openrouter/api_key": VAULT_KEY,
                    "providers/openrouter/api_key_3": VAULT_SPARE,
                }
            ),
        )

    def test_the_third_row_is_reported_at_position_three(self, gap_and_duplicate) -> None:
        slots = key_pool.provider_slots("openrouter")
        assert [s.position for s in slots] == [1, 3]

    def test_the_stranded_row_is_named_orphaned(self, gap_and_duplicate) -> None:
        slots = {s.position: s for s in key_pool.provider_slots("openrouter")}
        assert slots[1].state == "active"
        assert slots[3].state == "orphaned", "a key past a gap is never dialled — say so"

    def test_only_the_reachable_run_enters_the_pool(self, gap_and_duplicate) -> None:
        dialled = key_pool.resolve_keys("openrouter")
        assert [(d.position, d.key) for d in dialled] == [(1, VAULT_KEY)]

    def test_the_scan_sees_both_rows(self, gap_and_duplicate) -> None:
        assert [s.position for s in key_pool.scan_slots("openrouter")] == [1, 3]

    def test_a_duplicate_still_occupies_its_slot(self, monkeypatch) -> None:
        """Slot 2 duplicating slot 1 must not make slot 3 look stranded."""
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_3", ENV_SPARE)

        slots = key_pool.provider_slots("openrouter")
        assert [s.position for s in slots] == [1, 2, 3]
        assert all(s.state != "orphaned" for s in slots)
        # One credential in the pool per distinct value: rotating onto a copy
        # of the key that just failed buys nothing.
        assert [d.position for d in key_pool.resolve_keys("openrouter")] == [1, 3]

    def test_no_gap_means_no_orphans(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", ENV_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY_2", ENV_SPARE)
        assert [(s.position, s.state) for s in key_pool.provider_slots("openrouter")] == [
            (1, "active"),
            (2, "spare"),
        ]


class TestSnapshot:
    """The vault is read once per refresh, not once per credential lookup.

    ``api_key_for_model`` sits on the LLM hot path: every cache miss in
    ``pooled_completion`` resolves credentials. A per-slot ``vault.get`` would
    open a synchronous psycopg2 connection on the engine's event loop in the
    middle of a completion — five of them for one provider listing, and one per
    model call forever after.
    """

    @pytest.fixture
    def counted_vault(self, monkeypatch):
        from robothor.vault.naming import env_name

        calls: list[int] = []

        def _export() -> dict[str, str]:
            calls.append(1)
            return {env_name("providers/openrouter/api_key"): VAULT_KEY}

        monkeypatch.setattr(key_pool, "_vault_export", _export)
        return calls

    def test_the_llm_path_touches_the_database_once(self, counted_vault) -> None:
        for _ in range(5):
            assert key_pool.api_key_for_model("openrouter/openai/gpt-5.4") == VAULT_KEY
        assert len(counted_vault) == 1, f"vault read {len(counted_vault)}x on the LLM path"

    def test_a_full_provider_listing_costs_one_read(self, counted_vault) -> None:
        for spec in key_pool.PROVIDERS:
            key_pool.provider_slots(spec.id)
        assert len(counted_vault) == 1, "one connection per refresh, not one per slot"

    def test_a_refresh_re_reads_and_nothing_else_does(self, counted_vault) -> None:
        key_pool.vault_snapshot()
        key_pool.vault_snapshot()
        assert len(counted_vault) == 1
        key_pool.refresh_vault_snapshot()
        assert len(counted_vault) == 2

    def test_a_reload_picks_up_a_value_written_after_the_first_read(self, monkeypatch) -> None:
        """The snapshot must not become a stale cache the operator cannot clear."""
        from robothor.vault.naming import env_name

        state: dict[str, dict[str, str]] = {"keys": {}}
        monkeypatch.setattr(key_pool, "_vault_export", lambda: dict(state["keys"]))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert key_pool.resolve_keys("anthropic") == []

        state["keys"] = {env_name("providers/anthropic/api_key"): VAULT_KEY}
        key_pool.reload_provider_keys()
        assert [r.key for r in key_pool.resolve_keys("anthropic")] == [VAULT_KEY]

    def test_an_unreadable_vault_is_probed_once_not_per_lookup(self, monkeypatch) -> None:
        calls: list[int] = []

        def _explode() -> dict[str, str]:
            calls.append(1)
            raise FileNotFoundError("Vault master key not found")

        monkeypatch.setattr(key_pool, "_vault_export", _explode)
        key_pool.reset_vault_availability()
        for spec in key_pool.PROVIDERS:
            key_pool.resolve_keys(spec.id)
        assert len(calls) == 1


class TestSnapshotHoldsOnlyProviderKeys:
    """The snapshot is process-lifetime memory, so it holds as little as it can.

    ``export_env`` decrypts the WHOLE vault — Telegram tokens, SMTP passwords,
    whatever a plugin stored. Caching that dict kept every one of them
    plaintext in the engine for the life of the process, and handing the live
    dict to callers let any of them rewrite what the LLM path resolves.
    """

    @pytest.fixture
    def mixed_vault(self, monkeypatch):
        from robothor.vault.naming import channel_field, env_name, provider_key

        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {
                env_name(provider_key("openrouter")): VAULT_KEY,
                env_name(channel_field("telegram", "bot_token")): "telegram-bot-token-secret",
                "SMTP_PASSWORD": "smtp-password-secret",
            },
        )

    def test_a_non_provider_secret_never_enters_the_snapshot(self, mixed_vault) -> None:
        snapshot = key_pool.vault_snapshot()
        rendered = repr(snapshot)
        assert "telegram-bot-token-secret" not in rendered
        assert "smtp-password-secret" not in rendered

    def test_the_provider_key_is_still_there(self, mixed_vault) -> None:
        assert key_pool.resolve_keys("openrouter")[0].key == VAULT_KEY

    def test_the_caller_gets_a_copy(self, mixed_vault) -> None:
        first = key_pool.vault_snapshot()
        first["PROVIDERS_OPENROUTER_API_KEY"] = "tampered"
        first["INJECTED"] = "nope"
        assert key_pool.resolve_keys("openrouter")[0].key == VAULT_KEY
        assert "INJECTED" not in key_pool.vault_snapshot()

    def test_only_slots_the_pool_can_reach_are_kept(self, monkeypatch) -> None:
        from robothor.vault.naming import env_name, provider_key

        monkeypatch.setattr(
            key_pool,
            "_vault_export",
            lambda: {
                env_name(provider_key("openrouter")): VAULT_KEY,
                f"PROVIDERS_OPENROUTER_API_KEY_{key_pool.MAX_KEY_SLOTS + 1}": "beyond-the-walk",
            },
        )
        assert "beyond-the-walk" not in repr(key_pool.vault_snapshot())


class TestUnavailableVaultHeals:
    """Postgres is not always up before the engine is.

    A permanent latch meant a boot-order race — engine first, database a second
    later — left every provider credential unreadable for the life of the
    process, and the only symptom was one log line at startup.
    """

    @pytest.fixture
    def clock(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(key_pool, "_clock", lambda: now["t"])
        return now

    def test_it_retries_after_the_cooldown(self, monkeypatch, clock) -> None:
        from robothor.vault.naming import env_name, provider_key

        state = {"up": False}
        calls: list[int] = []

        def _export() -> dict[str, str]:
            calls.append(1)
            if not state["up"]:
                raise ConnectionError("could not connect to server")
            return {env_name(provider_key("openrouter")): VAULT_KEY}

        monkeypatch.setattr(key_pool, "_vault_export", _export)
        key_pool.reset_vault_availability()

        assert key_pool.resolve_keys("openrouter") == []
        assert len(calls) == 1

        # Still inside the cooldown: no second connection attempt.
        clock["t"] += key_pool.VAULT_RETRY_SECONDS - 1
        assert key_pool.resolve_keys("openrouter") == []
        assert len(calls) == 1

        state["up"] = True
        clock["t"] += 2
        assert key_pool.resolve_keys("openrouter")[0].key == VAULT_KEY
        assert len(calls) == 2

    def test_the_first_failure_is_a_warning_and_the_recovery_an_info(
        self, monkeypatch, clock, caplog
    ) -> None:
        from robothor.vault.naming import env_name, provider_key

        state = {"up": False}

        def _export() -> dict[str, str]:
            if not state["up"]:
                raise ConnectionError("could not connect to server")
            return {env_name(provider_key("openrouter")): VAULT_KEY}

        monkeypatch.setattr(key_pool, "_vault_export", _export)
        key_pool.reset_vault_availability()

        with caplog.at_level(logging.INFO, logger="robothor.engine.key_pool"):
            key_pool.resolve_keys("openrouter")
            for _ in range(3):
                key_pool.resolve_keys("openrouter")
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1, f"expected one WARNING, got {warnings}"

            caplog.clear()
            state["up"] = True
            clock["t"] += key_pool.VAULT_RETRY_SECONDS + 1
            key_pool.resolve_keys("openrouter")

        recoveries = [r for r in caplog.records if r.levelno == logging.INFO]
        assert recoveries, "a vault that comes back must say so"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_a_reload_retries_immediately_without_waiting(self, monkeypatch, clock) -> None:
        """An operator who has just fixed the database should not wait a minute."""
        calls: list[int] = []

        def _export() -> dict[str, str]:
            calls.append(1)
            raise ConnectionError("down")

        monkeypatch.setattr(key_pool, "_vault_export", _export)
        key_pool.reset_vault_availability()
        key_pool.resolve_keys("openrouter")
        assert len(calls) == 1
        key_pool.reload_provider_keys()
        assert len(calls) == 2
