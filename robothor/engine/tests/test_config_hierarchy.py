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
