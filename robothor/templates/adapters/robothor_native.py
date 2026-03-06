"""
Robothor native format adapter — the default and only adapter for now.

Generates:
  - docs/agents/<id>.yaml   (agent manifest)
  - brain/<INSTRUCTION>.md   (instruction file)

from template bundles with {{ variable }} resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robothor.templates.adapters.base import FormatAdapter
from robothor.templates.resolver import TemplateResolver


class RobothorNativeAdapter(FormatAdapter):
    """Adapter for the native Robothor agent format (YAML manifest + Markdown instructions)."""

    format_id = "robothor-native"

    def can_install(self, skill_md: dict) -> bool:
        fmt = str(skill_md.get("format", ""))
        return fmt.startswith("robothor-native") or fmt == ""

    def generate_files(self, bundle_path: Path, variables: dict[str, Any]) -> dict[str, str]:
        """Resolve template files and return their contents."""
        resolver = TemplateResolver()
        result = {}

        manifest_template = bundle_path / "manifest.template.yaml"
        if manifest_template.exists():
            result["manifest.yaml"] = resolver.resolve_file(manifest_template, variables)

        instructions_template = bundle_path / "instructions.template.md"
        if instructions_template.exists():
            result["instructions.md"] = resolver.resolve_file(instructions_template, variables)

        return result

    def validate(self, bundle_path: Path) -> list[str]:
        """Validate a Robothor native template bundle."""
        from robothor.templates.validators import validate_bundle

        errors = validate_bundle(bundle_path)
        return [str(e) for e in errors]

    def get_output_paths(self, manifest: dict) -> dict[str, str]:
        """Determine output paths from a resolved manifest."""
        agent_id = manifest.get("id", "unknown")
        instruction_file = manifest.get("instruction_file", "")

        return {
            "manifest": f"docs/agents/{agent_id}.yaml",
            "instruction": instruction_file,
        }
