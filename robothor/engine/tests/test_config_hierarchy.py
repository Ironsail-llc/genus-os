"""Tests for config hierarchy — project overrides, env overrides, validation, explain, conditional."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.config import (
    _apply_conditional_config,
    _coerce_value,
    _collect_env_overrides,
    _get_runtime_overrides,
    _load_project_config,
    _merge_lifecycle_hooks,
    explain_config,
    set_runtime_overrides,
)
from robothor.engine.config_schema import validate_manifest

# ─── Project Config ──────────────────────────────────────────────────


class TestProjectConfig:
    def test_project_config_loads(self, tmp_path: Path):
        """Loads .robothor/config.yaml from workspace."""
        cfg_dir = tmp_path / ".robothor"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.yaml"
        cfg_file.write_text(yaml.dump({"_all": {"v2": {"safety_cap": 100}}}))

        # Clear the module-level cache before testing
        import robothor.engine.config as config_mod

        old_cache = config_mod._project_config_cache
        config_mod._project_config_cache = (0.0, {})
        try:
            result = _load_project_config(tmp_path)
            assert result == {"_all": {"v2": {"safety_cap": 100}}}
        finally:
            config_mod._project_config_cache = old_cache

    def test_project_config_missing(self, tmp_path: Path):
        """Returns {} when .robothor/config.yaml does not exist."""
        result = _load_project_config(tmp_path)
        assert result == {}


# ─── Env Overrides ───────────────────────────────────────────────────


class TestEnvOverrides:
    def test_env_overrides_simple(self, monkeypatch):
        """ROBOTHOR_OVERRIDE_V2__MAX_ITERATIONS=30 -> {'v2': {'max_iterations': 30}}."""
        monkeypatch.setenv("ROBOTHOR_OVERRIDE_V2__MAX_ITERATIONS", "30")
        result = _collect_env_overrides()
        assert result == {"v2": {"max_iterations": 30}}

    def test_env_overrides_bool(self, monkeypatch):
        """ROBOTHOR_OVERRIDE_V2__CONTINUOUS=true -> {'v2': {'continuous': True}}."""
        monkeypatch.setenv("ROBOTHOR_OVERRIDE_V2__CONTINUOUS", "true")
        result = _collect_env_overrides()
        assert result.get("v2", {}).get("continuous") is True


# ─── Coerce Value ────────────────────────────────────────────────────


class TestCoerceValue:
    def test_coerce_true(self):
        assert _coerce_value("true") is True

    def test_coerce_false(self):
        assert _coerce_value("false") is False

    def test_coerce_int(self):
        assert _coerce_value("42") == 42

    def test_coerce_float(self):
        assert _coerce_value("3.14") == 3.14

    def test_coerce_string(self):
        assert _coerce_value("hello") == "hello"


# ─── Conditional Config ──────────────────────────────────────────────


class TestConditionalConfig:
    def test_apply_conditional_config(self):
        """When trigger_type matches a clause, overrides are applied."""
        data = {
            "id": "test",
            "schedule": {"max_iterations": 10},
            "when": [
                {
                    "trigger_type": "cron",
                    "overrides": {"schedule": {"max_iterations": 50}},
                },
            ],
        }
        result = _apply_conditional_config(data, "cron")
        assert result["schedule"]["max_iterations"] == 50
        assert "when" not in result

    def test_apply_conditional_no_match(self):
        """When trigger_type doesn't match any clause, no changes."""
        data = {
            "id": "test",
            "schedule": {"max_iterations": 10},
            "when": [
                {
                    "trigger_type": "cron",
                    "overrides": {"schedule": {"max_iterations": 50}},
                },
            ],
        }
        result = _apply_conditional_config(data, "telegram")
        assert result["schedule"]["max_iterations"] == 10
        assert "when" not in result


# ─── Lifecycle Hooks Merge ───────────────────────────────────────────


class TestMergeLifecycleHooks:
    def test_merge_lifecycle_hooks_dedup(self):
        """Fleet hooks + agent hooks with same (event, handler) — agent wins."""
        defaults = {
            "v2": {
                "lifecycle_hooks": [
                    {"event": "agent_start", "handler": "fleet_handler", "priority": 100},
                ],
            },
        }
        merged = {
            "v2": {
                "lifecycle_hooks": [
                    {"event": "agent_start", "handler": "fleet_handler", "priority": 10},
                ],
            },
        }
        _merge_lifecycle_hooks(merged, defaults)
        hooks = merged["v2"]["lifecycle_hooks"]
        # Only one hook — agent version wins (priority=10, not fleet's 100)
        assert len(hooks) == 1
        assert hooks[0]["priority"] == 10

    def test_merge_lifecycle_hooks_concat(self):
        """Fleet hooks + agent hooks with different keys — both present."""
        defaults = {
            "v2": {
                "lifecycle_hooks": [
                    {"event": "agent_start", "handler": "fleet_handler"},
                ],
            },
        }
        merged = {
            "v2": {
                "lifecycle_hooks": [
                    {"event": "agent_end", "handler": "agent_handler"},
                ],
            },
        }
        _merge_lifecycle_hooks(merged, defaults)
        hooks = merged["v2"]["lifecycle_hooks"]
        assert len(hooks) == 2
        events = {h["event"] for h in hooks}
        assert events == {"agent_start", "agent_end"}


# ─── Validate Manifest ──────────────────────────────────────────────


class TestValidateManifest:
    def test_validate_manifest_valid(self):
        """Valid manifest returns empty warnings list."""
        data = {
            "id": "test-agent",
            "schedule": {"max_iterations": 20, "timeout_seconds": 600},
            "delivery": {"mode": "none"},
            "v2": {"safety_cap": 200},
        }
        warnings = validate_manifest(data)
        assert warnings == []

    def test_validate_manifest_unknown_v2_key(self):
        """Manifest with typo v2 key returns warning."""
        data = {
            "id": "test-agent",
            "v2": {"planing_enabled": True},  # typo: planing vs planning
        }
        warnings = validate_manifest(data)
        assert any("planing_enabled" in w for w in warnings)

    def test_validate_manifest_unknown_guardrail(self):
        """Unknown guardrail name produces warning."""
        data = {
            "id": "test-agent",
            "v2": {"guardrails": ["no_destructive_writes", "fake_guardrail"]},
        }
        warnings = validate_manifest(data)
        assert any("fake_guardrail" in w for w in warnings)

    def test_validate_manifest_range_error(self):
        """Out-of-range safety_cap produces warning."""
        data = {
            "id": "test-agent",
            "v2": {"safety_cap": 99999},
        }
        warnings = validate_manifest(data)
        assert any("safety_cap" in w for w in warnings)

    def test_max_iterations_zero_is_the_documented_unlimited_value(self):
        """0 means "no check-in interval" — main runs on it, so it must not warn.

        ``manifest_to_agent_config`` clamps only NEGATIVE values (the run loop
        guards with ``_checkin_interval > 0``), and safety_cap is what actually
        bounds the run. Warning on 0 put a line in the log on every schedule
        tick for a value the schema documents.
        """
        warnings = validate_manifest({"id": "test-agent", "schedule": {"max_iterations": 0}})
        assert not any("max_iterations" in w for w in warnings)

    def test_negative_max_iterations_still_warns(self):
        """0 is a sentinel; -1 is a mistake."""
        warnings = validate_manifest({"id": "test-agent", "schedule": {"max_iterations": -1}})
        assert any("max_iterations" in w for w in warnings)


class TestValidationWarningLogging:
    """The loader runs on every schedule tick; the warnings do not change."""

    MANIFEST = {
        "id": "agent-a",
        "name": "Agent A",
        "v2": {"planing_enabled": True},  # typo → one stable warning
    }

    def _write(self, tmp_path: Path) -> Path:
        (tmp_path / "agent-a.yaml").write_text(yaml.dump(self.MANIFEST))
        return tmp_path

    def test_repeated_loads_log_the_warning_once(self, tmp_path: Path, caplog):
        """Same agent, same warning, same process → one log record, not one per tick."""
        import logging

        import robothor.engine.config as config_mod

        manifest_dir = self._write(tmp_path)
        config_mod.reset_validation_warning_log()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            config_mod.load_agent_config("agent-a", manifest_dir)
            config_mod.load_agent_config("agent-a", manifest_dir)

        records = [r for r in caplog.records if "planing_enabled" in r.getMessage()]
        assert len(records) == 1

    def test_deduping_does_not_hide_the_warnings_from_callers(self, tmp_path: Path):
        """Only the LOG is deduped — config.validation_warnings stays complete."""
        import robothor.engine.config as config_mod

        manifest_dir = self._write(tmp_path)
        config_mod.reset_validation_warning_log()
        first = config_mod.load_agent_config("agent-a", manifest_dir)
        second = config_mod.load_agent_config("agent-a", manifest_dir)

        assert first is not None and second is not None
        assert any("planing_enabled" in w for w in first.validation_warnings)
        assert second.validation_warnings == first.validation_warnings

    def test_a_different_agent_still_logs(self, tmp_path: Path, caplog):
        """Dedupe is per (agent, warning) — a second agent's copy is its own news."""
        import logging

        import robothor.engine.config as config_mod

        manifest_dir = self._write(tmp_path)
        other = dict(self.MANIFEST, id="agent-b", name="Agent B")
        (manifest_dir / "agent-b.yaml").write_text(yaml.dump(other))

        config_mod.reset_validation_warning_log()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            config_mod.load_agent_config("agent-a", manifest_dir)
            config_mod.load_agent_config("agent-b", manifest_dir)

        records = [r for r in caplog.records if "planing_enabled" in r.getMessage()]
        assert len(records) == 2

    def test_a_new_warning_for_a_seen_agent_still_logs(self, tmp_path: Path, caplog):
        """Suppression is keyed on the text, so a manifest that gets WORSE is not silent."""
        import logging

        import robothor.engine.config as config_mod

        manifest_dir = self._write(tmp_path)
        config_mod.reset_validation_warning_log()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            config_mod.load_agent_config("agent-a", manifest_dir)
            worse = dict(self.MANIFEST, v2={"planing_enabled": True, "guardrails": ["not_real"]})
            (manifest_dir / "agent-a.yaml").write_text(yaml.dump(worse))
            config_mod.load_agent_config("agent-a", manifest_dir)

        assert sum("not_real" in r.getMessage() for r in caplog.records) == 1
        assert sum("planing_enabled" in r.getMessage() for r in caplog.records) == 1

    def test_the_dedupe_key_names_the_agent_the_log_line_names(
        self, tmp_path: Path, caplog, monkeypatch
    ):
        """Key and log line must agree on the id, or they dedupe different things."""
        import logging

        import robothor.engine.config as config_mod

        monkeypatch.setattr(config_mod, "_sanitize", lambda value: f"scrubbed-{value}")
        manifest_dir = self._write(tmp_path)
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            config_mod.load_agent_config("agent-a", manifest_dir)

        record = next(r for r in caplog.records if "planing_enabled" in r.getMessage())
        assert "scrubbed-agent-a" in record.getMessage()
        assert {key[0] for key in config_mod._logged_validation_warnings} == {"scrubbed-agent-a"}


class TestValidationWarningLogIsolation:
    """The dedupe set is a module GLOBAL, so it leaks between tests.

    Whichever test runs second used to see zero records — the first test's
    entry was still in ``_logged_validation_warnings`` — and the honest way to
    prove the reset works is two tests that neither know about each other nor
    call the reset themselves. The autouse fixture in the repo conftest is
    what makes both pass.
    """

    MANIFEST = {
        "id": "agent-a",
        "name": "Agent A",
        "v2": {"planing_enabled": True},  # typo → one stable warning
    }

    def _load_and_count(self, tmp_path: Path, caplog) -> int:
        import logging

        import robothor.engine.config as config_mod

        (tmp_path / "agent-a.yaml").write_text(yaml.dump(self.MANIFEST))
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            config_mod.load_agent_config("agent-a", tmp_path)
        return sum("planing_enabled" in r.getMessage() for r in caplog.records)

    def test_first_test_logs_the_warning_once(self, tmp_path: Path, caplog):
        assert self._load_and_count(tmp_path, caplog) == 1

    def test_second_test_logs_the_warning_once_too(self, tmp_path: Path, caplog):
        """Identical to the test above — and that is the point."""
        assert self._load_and_count(tmp_path, caplog) == 1


# ─── Explain Config ──────────────────────────────────────────────────


class TestExplainConfig:
    def test_explain_config_returns_attribution(self, tmp_path: Path):
        """explain_config returns layers, merged, and attribution dict."""
        manifest_dir = tmp_path / "docs" / "agents"
        manifest_dir.mkdir(parents=True)

        manifest = {
            "id": "test-agent",
            "name": "Test Agent",
            "model": {"primary": "test-model"},
            "schedule": {"cron": "0 * * * *"},
        }
        (manifest_dir / "test-agent.yaml").write_text(yaml.dump(manifest))

        result = explain_config("test-agent", manifest_dir, workspace=tmp_path)
        assert result["agent_id"] == "test-agent"
        assert "merged" in result
        assert "attribution" in result
        assert isinstance(result["attribution"], dict)
        # The agent manifest should provide the 'id' attribution
        assert "id" in result["attribution"]


# ─── Runtime Overrides ───────────────────────────────────────────────


class TestRuntimeOverrides:
    def test_runtime_overrides(self):
        """set_runtime_overrides stores values retrievable by _get_runtime_overrides."""
        import robothor.engine.config as config_mod

        old = config_mod._runtime_overrides
        try:
            set_runtime_overrides({"v2": {"safety_cap": 500}})
            result = _get_runtime_overrides()
            assert result == {"v2": {"safety_cap": 500}}
        finally:
            config_mod._runtime_overrides = old


class TestValidateManifestModelBlock:
    """Every model a manifest names must exist in the registry.

    Nothing checked this before, and it let a fallback chain end in fiction:
    docs/agents/_defaults.yaml and main.yaml named ollama_chat/qwen3.8:27b as
    the last-resort tier for ~30 hours while no server anywhere could serve it
    — litellm routes ollama_chat/* to :11434, where the model did not exist.
    During a real OpenRouter outage the chain would have spent 2 x 600s
    (LLM_REQUEST_TIMEOUT_OLLAMA) connecting to nothing and then raised. The
    dead tier made outages twenty minutes SLOWER, and no log line said why.

    A typo'd model name is exactly as silent: get_model_limits falls back to a
    generic 128K limit with a warning nobody reads, and the model is simply
    skipped at dispatch.
    """

    def test_unknown_primary_model_warns(self):
        data = {"id": "a", "model": {"primary": "openrouter/nonexistent/model-xyz"}}
        warnings = validate_manifest(data)
        assert any("nonexistent/model-xyz" in w for w in warnings), warnings

    def test_unknown_fallback_model_warns(self):
        data = {
            "id": "a",
            "model": {
                "primary": "openrouter/xiaomi/mimo-v2.5",
                "fallbacks": ["ollama_chat/qwen99:1b"],
            },
        }
        warnings = validate_manifest(data)
        assert any("qwen99:1b" in w for w in warnings), warnings

    def test_registered_models_do_not_warn(self):
        data = {
            "id": "a",
            "model": {
                "primary": "openrouter/xiaomi/mimo-v2.5",
                "fallbacks": [
                    "openrouter/anthropic/claude-sonnet-4.6",
                    "ollama_chat/qwen3.8:27b",
                ],
            },
        }
        warnings = validate_manifest(data)
        assert not any("not in the model registry" in w for w in warnings), warnings

    def test_heartbeat_and_worker_model_blocks_are_checked(self):
        """The incident file's broken entry was in the HEARTBEAT model block —
        a top-level-only check would have missed it."""
        data = {
            "id": "a",
            "model": {"primary": "openrouter/xiaomi/mimo-v2.5"},
            "heartbeat": {"model": {"fallbacks": ["ollama_chat/bogus:27b"]}},
            "worker": {"model": {"primary": "openrouter/fake/worker-model"}},
        }
        warnings = validate_manifest(data)
        assert any("bogus:27b" in w for w in warnings), warnings
        assert any("fake/worker-model" in w for w in warnings), warnings

    def test_empty_model_block_is_fine(self):
        assert not any("model registry" in w for w in validate_manifest({"id": "a"}))

    def test_env_var_placeholders_are_not_flagged(self):
        """Manifests may reference ${VARS} resolved at load time; an unresolved
        placeholder is a different problem than an unknown model."""
        data = {"id": "a", "model": {"primary": "${ROBOTHOR_PRIMARY_MODEL}"}}
        warnings = validate_manifest(data)
        assert not any("model registry" in w for w in warnings), warnings
