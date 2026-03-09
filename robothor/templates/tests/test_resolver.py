"""Tests for the template variable resolution engine."""

import pytest
import yaml

from robothor.templates.resolver import (
    TemplateResolver,
    deep_merge,
    find_unresolved,
    resolve_string,
    resolve_value,
)


class TestResolveString:
    def test_simple_variable(self):
        result = resolve_string("{{ model }}", {"model": "gpt-4"})
        assert result == "gpt-4"

    def test_variable_with_spaces(self):
        result = resolve_string("{{  model  }}", {"model": "gpt-4"})
        assert result == "gpt-4"

    def test_multiple_variables(self):
        result = resolve_string(
            "{{ model }} in {{ timezone }}",
            {"model": "gpt-4", "timezone": "UTC"},
        )
        assert result == "gpt-4 in UTC"

    def test_unresolved_variable_preserved(self):
        result = resolve_string("{{ unknown }}", {})
        assert result == "{{ unknown }}"

    def test_env_var_preserved(self):
        """${ENV_VAR} patterns must NOT be resolved."""
        result = resolve_string("${TELEGRAM_CHAT_ID}", {"TELEGRAM_CHAT_ID": "12345"})
        assert result == "${TELEGRAM_CHAT_ID}"

    def test_mixed_template_and_env_var(self):
        result = resolve_string(
            "model: {{ model }}, chat: ${CHAT_ID}",
            {"model": "gpt-4"},
        )
        assert result == "model: gpt-4, chat: ${CHAT_ID}"

    def test_none_value_becomes_empty(self):
        result = resolve_string("{{ value }}", {"value": None})
        assert result == ""

    def test_integer_value(self):
        result = resolve_string("port: {{ port }}", {"port": 8080})
        assert result == "port: 8080"

    def test_boolean_value(self):
        result = resolve_string("{{ enabled }}", {"enabled": True})
        assert result == "True"

    def test_expression_eval(self):
        """Simple expressions are evaluated."""
        result = resolve_string("{{ x + 1 }}", {"x": 5})
        assert result == "6"


class TestFilters:
    def test_default_filter_with_value(self):
        result = resolve_string('{{ model | default("fallback") }}', {"model": "gpt-4"})
        assert result == "gpt-4"

    def test_default_filter_without_value(self):
        result = resolve_string('{{ model | default("fallback") }}', {"model": ""})
        assert result == "fallback"

    def test_default_filter_none(self):
        result = resolve_string('{{ model | default("fallback") }}', {"model": None})
        assert result == "fallback"

    def test_upper_filter(self):
        result = resolve_string("{{ name | upper }}", {"name": "hello"})
        assert result == "HELLO"

    def test_lower_filter(self):
        result = resolve_string("{{ name | lower }}", {"name": "HELLO"})
        assert result == "hello"

    def test_title_case_filter(self):
        result = resolve_string("{{ id | title_case }}", {"id": "email-classifier"})
        assert result == "Email Classifier"

    def test_upper_snake_case_filter(self):
        result = resolve_string("{{ id | upper_snake_case }}", {"id": "email-classifier"})
        assert result == "EMAIL_CLASSIFIER"

    def test_kebab_case_filter(self):
        result = resolve_string("{{ name | kebab_case }}", {"name": "Email Classifier"})
        assert result == "email-classifier"


class TestResolveValue:
    def test_string(self):
        assert resolve_value("{{ x }}", {"x": "hello"}) == "hello"

    def test_list(self):
        result = resolve_value(["{{ a }}", "{{ b }}"], {"a": "1", "b": "2"})
        assert result == ["1", "2"]

    def test_dict(self):
        result = resolve_value({"key": "{{ v }}"}, {"v": "val"})
        assert result == {"key": "val"}

    def test_nested(self):
        data = {"outer": {"inner": "{{ x }}"}}
        result = resolve_value(data, {"x": "deep"})
        assert result == {"outer": {"inner": "deep"}}

    def test_non_string_passthrough(self):
        assert resolve_value(42, {}) == 42
        assert resolve_value(True, {}) is True


class TestDeepMerge:
    def test_simple_override(self):
        result = deep_merge({"a": 1}, {"a": 2})
        assert result == {"a": 2}

    def test_add_new_key(self):
        result = deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_nested_dict_merge(self):
        base = {"model": {"primary": "gpt-4", "temp": 0.7}}
        override = {"model": {"primary": "claude"}}
        result = deep_merge(base, override)
        assert result == {"model": {"primary": "claude", "temp": 0.7}}

    def test_list_dedup(self):
        base = {"tools": ["a", "b"]}
        override = {"tools": ["b", "c"]}
        result = deep_merge(base, override)
        assert result == {"tools": ["a", "b", "c"]}

    def test_no_mutation(self):
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        deep_merge(base, override)
        assert "c" not in base["a"]


class TestFindUnresolved:
    def test_no_unresolved(self):
        assert find_unresolved("hello world") == []

    def test_single_unresolved(self):
        assert find_unresolved("{{ missing }}") == ["missing"]

    def test_multiple_unresolved(self):
        result = find_unresolved("{{ a }} and {{ b }}")
        assert set(result) == {"a", "b"}


class TestTemplateResolver:
    def test_build_context_priority(self):
        resolver = TemplateResolver()
        context = resolver.build_context(
            setup_yaml={
                "variables": {
                    "model": {"type": "string", "default": "setup-model"},
                    "timezone": {"type": "string", "default": "UTC"},
                }
            },
            defaults_yaml={"model": "default-model", "extra": "default-extra"},
            overrides={"model": "override-model"},
        )
        # Override wins over setup default which wins over defaults
        assert context["model"] == "override-model"
        assert context["timezone"] == "UTC"
        assert context["extra"] == "default-extra"

    def test_build_context_cli_wins(self):
        resolver = TemplateResolver()
        context = resolver.build_context(
            setup_yaml={"variables": {"x": {"default": "setup"}}},
            defaults_yaml={"x": "defaults"},
            overrides={"x": "override"},
            cli_sets={"x": "cli"},
        )
        assert context["x"] == "cli"

    def test_resolve_bundle(self, tmp_bundle, tmp_defaults):
        resolver = TemplateResolver()
        result = resolver.resolve_bundle(
            str(tmp_bundle),
            variables={"version": "1.0.0"},
            defaults_path=str(tmp_defaults),
        )
        assert "manifest.yaml" in result
        assert "instructions.md" in result

        # Check manifest resolved correctly
        manifest = yaml.safe_load(result["manifest.yaml"])
        assert manifest["id"] == "test-agent"
        assert manifest["model"]["primary"] == "openrouter/z-ai/glm-5"
        assert manifest["schedule"]["timezone"] == "America/New_York"

    def test_resolve_bundle_with_overrides(self, tmp_bundle):
        resolver = TemplateResolver()
        result = resolver.resolve_bundle(
            str(tmp_bundle),
            variables={
                "version": "2.0.0",
                "model_primary": "openrouter/anthropic/claude-sonnet-4.6",
                "timezone": "Europe/London",
            },
        )
        manifest = yaml.safe_load(result["manifest.yaml"])
        assert manifest["model"]["primary"] == "openrouter/anthropic/claude-sonnet-4.6"
        assert manifest["schedule"]["timezone"] == "Europe/London"

    def test_resolve_dry_run(self, tmp_bundle):
        resolver = TemplateResolver()
        result = resolver.resolve_dry_run(
            str(tmp_bundle),
            variables={"version": "1.0.0"},
        )
        assert result["clean"] is True
        assert result["unresolved"] == {}

    def test_resolve_dry_run_missing_var(self, tmp_path):
        """Dry run with missing required variable shows unresolved."""
        bundle = tmp_path / "incomplete"
        bundle.mkdir()
        (bundle / "setup.yaml").write_text("agent_id: test\nvariables: {}")
        (bundle / "manifest.template.yaml").write_text("id: {{ missing_var }}")

        resolver = TemplateResolver()
        result = resolver.resolve_dry_run(str(bundle))
        assert result["clean"] is False
        assert "manifest.yaml" in result["unresolved"]

    def test_bundle_not_found(self):
        resolver = TemplateResolver()
        with pytest.raises(FileNotFoundError):
            resolver.resolve_bundle("/nonexistent/path")
