"""
Template variable resolution engine.

Resolves {{ variable }} patterns in template files (YAML manifests and Markdown
instruction files). Uses the same regex as robothor/engine/workflow.py for
consistency.

Key design:
  - {{ variable }} is resolved at install time
  - ${ENV_VAR} patterns are PRESERVED (engine resolves these at runtime)
  - Supports filters: {{ var | default("value") }}, {{ var | upper }}, etc.
  - Resolution priority (last wins):
    _defaults.yaml -> setup.yaml defaults -> .robothor/config.yaml
    -> .robothor/overrides/<id>.yaml -> CLI --set key=value
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# Same regex as workflow.py:49
TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")

# ${ENV_VAR} pattern — must NOT be resolved (engine handles at runtime)
ENV_VAR_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")

# Built-in filters
FILTERS: dict[str, Callable[..., str]] = {
    "upper": lambda v: str(v).upper(),
    "lower": lambda v: str(v).lower(),
    "title_case": lambda v: str(v).replace("-", " ").title(),
    "upper_snake_case": lambda v: str(v).upper().replace("-", "_"),
    "kebab_case": lambda v: str(v).lower().replace("_", "-").replace(" ", "-"),
}


def _parse_filter_chain(expr: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Parse 'var | filter1 | filter2("arg")' into (var, [(filter, args)])."""
    parts = expr.split("|")
    var_expr = parts[0].strip()
    filters = []
    for part in parts[1:]:
        part = part.strip()
        # Match filter_name or filter_name("arg") or filter_name("arg1", "arg2")
        m = re.match(r"(\w+)(?:\((.+)\))?$", part)
        if m:
            name = m.group(1)
            args_str = m.group(2)
            args = []
            if args_str:
                # Parse quoted string args
                args = [a.strip().strip("'\"") for a in args_str.split(",")]
            filters.append((name, args))
    return var_expr, filters


def _apply_filters(value: Any, filter_chain: list[tuple[str, list[str]]]) -> Any:
    """Apply a chain of filters to a value."""
    for name, args in filter_chain:
        if name == "default":
            if value is None or value == "":
                value = args[0] if args else ""
        elif name in FILTERS:
            value = FILTERS[name](value)
        # Unknown filters are silently ignored
    return value


def resolve_string(template: str, context: dict[str, Any]) -> str:
    """Resolve {{ expr }} patterns in a string, preserving ${ENV_VAR} patterns.

    Uses restricted eval (no builtins) for expressions, same as workflow.py.
    """

    def _replace(match: re.Match[str]) -> str:
        raw_expr = match.group(1)
        var_expr, filter_chain = _parse_filter_chain(raw_expr)

        try:
            result = eval(var_expr, {"__builtins__": {}}, context)
        except Exception:
            # Unresolved variable — return original placeholder
            return match.group(0)

        result = _apply_filters(result, filter_chain)
        return str(result) if result is not None else ""

    return TEMPLATE_RE.sub(_replace, template)


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """Recursively resolve templates in any YAML value (str, list, dict)."""
    if isinstance(value, str):
        return resolve_string(value, context)
    elif isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    elif isinstance(value, dict):
        return {k: resolve_value(v, context) for k, v in value.items()}
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge two dicts. Override wins for scalars, recurse for dicts, deduplicate lists."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            # Deduplicate while preserving order
            seen = set()
            merged = []
            for item in result[key] + value:
                item_key = str(item)
                if item_key not in seen:
                    seen.add(item_key)
                    merged.append(item)
            result[key] = merged
        else:
            result[key] = copy.deepcopy(value)
    return result


def find_unresolved(text: str) -> list[str]:
    """Find any {{ variable }} patterns that weren't resolved."""
    return TEMPLATE_RE.findall(text)


class TemplateResolver:
    """Resolves template bundles into ready-to-use agent manifests."""

    def build_context(
        self,
        setup_yaml: dict,
        defaults_yaml: dict | None = None,
        instance_config: dict | None = None,
        overrides: dict | None = None,
        cli_sets: dict | None = None,
    ) -> dict[str, Any]:
        """Build variable context with proper priority.

        Resolution priority (last wins):
          _defaults.yaml -> setup.yaml defaults -> .robothor/config.yaml
          -> .robothor/overrides/<id>.yaml -> CLI --set key=value
        """
        context: dict[str, Any] = {}

        # 1. Global defaults from _defaults.yaml
        if defaults_yaml:
            context.update(defaults_yaml)

        # 2. Template defaults from setup.yaml
        variables = setup_yaml.get("variables", {})
        for var_name, var_def in variables.items():
            if isinstance(var_def, dict) and "default" in var_def:
                context[var_name] = var_def["default"]
            elif not isinstance(var_def, dict):
                # Simple key: value
                context[var_name] = var_def

        # 3. Instance config defaults
        if instance_config:
            instance_defaults = instance_config.get("defaults", {})
            context.update(instance_defaults)
            # Also merge instance-level values
            inst = instance_config.get("instance", {})
            for k, v in inst.items():
                context[k] = v

        # 4. Per-agent overrides
        if overrides:
            context.update(overrides)

        # 5. CLI --set overrides (highest priority)
        if cli_sets:
            context.update(cli_sets)

        return context

    def resolve_file(self, file_path: Path, context: dict[str, Any]) -> str:
        """Resolve a single template file and return its content."""
        content = file_path.read_text()
        return resolve_string(content, context)

    def resolve_yaml_file(self, file_path: Path, context: dict[str, Any]) -> dict:
        """Resolve a YAML template file and return parsed dict."""
        content = self.resolve_file(file_path, context)
        return yaml.safe_load(content) or {}

    def resolve_bundle(
        self,
        bundle_path: str | Path,
        variables: dict[str, Any] | None = None,
        defaults_path: str | Path | None = None,
        instance_config: dict | None = None,
    ) -> dict[str, str]:
        """Resolve an entire template bundle.

        Returns dict mapping output filenames to resolved content:
          {"manifest.yaml": "...", "instructions.md": "..."}
        """
        bundle = Path(bundle_path)
        if not bundle.is_dir():
            raise FileNotFoundError(f"Template bundle not found: {bundle}")

        # Load setup.yaml
        setup_path = bundle / "setup.yaml"
        if not setup_path.exists():
            raise FileNotFoundError(f"setup.yaml not found in {bundle}")
        setup = yaml.safe_load(setup_path.read_text()) or {}

        # Load global defaults
        defaults = None
        if defaults_path:
            dp = Path(defaults_path)
            if dp.exists():
                defaults = yaml.safe_load(dp.read_text()) or {}

        # Build context
        context = self.build_context(
            setup_yaml=setup,
            defaults_yaml=defaults,
            instance_config=instance_config,
            overrides=variables,
        )

        # Resolve template files
        result = {}

        manifest_template = bundle / "manifest.template.yaml"
        if manifest_template.exists():
            result["manifest.yaml"] = self.resolve_file(manifest_template, context)

        instructions_template = bundle / "instructions.template.md"
        if instructions_template.exists():
            result["instructions.md"] = self.resolve_file(instructions_template, context)

        return result

    def resolve_dry_run(
        self,
        bundle_path: str | Path,
        variables: dict[str, Any] | None = None,
        defaults_path: str | Path | None = None,
        instance_config: dict | None = None,
    ) -> dict[str, Any]:
        """Dry-run resolve: returns resolved content + any unresolved variables."""
        files = self.resolve_bundle(bundle_path, variables, defaults_path, instance_config)

        unresolved = {}
        for filename, content in files.items():
            remaining = find_unresolved(content)
            if remaining:
                unresolved[filename] = remaining

        return {
            "files": files,
            "unresolved": unresolved,
            "clean": len(unresolved) == 0,
        }
