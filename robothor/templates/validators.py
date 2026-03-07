"""
Template-specific validation — validates template bundles before and after install.

Pre-install:
  - SKILL.md frontmatter (required fields: name, version, description, format)
  - setup.yaml schema (variables have type + default)
  - manifest.template.yaml resolves without errors

Post-install:
  - Delegates to the 12 checks A-L in manifest_checks.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


class ValidationError:
    """A single validation issue."""

    def __init__(self, level: str, message: str, file: str = ""):
        self.level = level  # "error" or "warning"
        self.message = message
        self.file = file

    def __repr__(self) -> str:
        prefix = f"[{self.file}] " if self.file else ""
        return f"{prefix}{self.level.upper()}: {self.message}"


def validate_skill_md(bundle_path: Path) -> list[ValidationError]:
    """Validate SKILL.md frontmatter has required fields."""
    errors = []
    skill_path = bundle_path / "SKILL.md"

    if not skill_path.exists():
        errors.append(
            ValidationError("warning", "SKILL.md not found (hub-ready metadata)", "SKILL.md")
        )
        return errors

    content = skill_path.read_text()

    # Parse YAML frontmatter (between --- markers)
    frontmatter_match = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
    if not frontmatter_match:
        errors.append(ValidationError("error", "No YAML frontmatter found", "SKILL.md"))
        return errors

    try:
        frontmatter = yaml.safe_load(frontmatter_match.group(1)) or {}
    except yaml.YAMLError as e:
        errors.append(ValidationError("error", f"Invalid YAML frontmatter: {e}", "SKILL.md"))
        return errors

    required = ["name", "version", "description", "format"]
    errors.extend(
        ValidationError("error", f"Missing required field: {field}", "SKILL.md")
        for field in required
        if field not in frontmatter or not frontmatter[field]
    )

    return errors


def validate_setup_yaml(bundle_path: Path) -> list[ValidationError]:
    """Validate setup.yaml schema — variables have type + default."""
    errors = []
    setup_path = bundle_path / "setup.yaml"

    if not setup_path.exists():
        errors.append(ValidationError("error", "setup.yaml not found", "setup.yaml"))
        return errors

    try:
        setup = yaml.safe_load(setup_path.read_text()) or {}
    except yaml.YAMLError as e:
        errors.append(ValidationError("error", f"Invalid YAML: {e}", "setup.yaml"))
        return errors

    # Must have a variables section
    variables = setup.get("variables", {})
    if not variables:
        errors.append(ValidationError("warning", "No variables defined", "setup.yaml"))
        return errors

    for var_name, var_def in variables.items():
        if not isinstance(var_def, dict):
            continue  # Simple key: value pairs are ok
        if "type" not in var_def:
            errors.append(
                ValidationError("warning", f"Variable '{var_name}' has no type", "setup.yaml")
            )
        if "default" not in var_def and not var_def.get("required", False):
            errors.append(
                ValidationError(
                    "warning",
                    f"Variable '{var_name}' has no default and is not required",
                    "setup.yaml",
                )
            )

    return errors


def validate_template_resolves(
    bundle_path: Path,
    context: dict[str, Any] | None = None,
) -> list[ValidationError]:
    """Validate that manifest.template.yaml resolves without errors."""
    from robothor.templates.resolver import TemplateResolver, find_unresolved

    errors = []
    resolver = TemplateResolver()

    manifest_template = bundle_path / "manifest.template.yaml"
    if not manifest_template.exists():
        errors.append(
            ValidationError("error", "manifest.template.yaml not found", "manifest.template.yaml")
        )
        return errors

    # Build context from setup.yaml defaults if none provided
    if context is None:
        setup_path = bundle_path / "setup.yaml"
        if setup_path.exists():
            setup = yaml.safe_load(setup_path.read_text()) or {}
            context = resolver.build_context(setup_yaml=setup)
        else:
            context = {}

    # Try to resolve
    try:
        content = resolver.resolve_file(manifest_template, context)
    except Exception as e:
        errors.append(ValidationError("error", f"Resolution failed: {e}", "manifest.template.yaml"))
        return errors

    # Check for unresolved variables
    unresolved = find_unresolved(content)
    if unresolved:
        errors.extend(
            ValidationError(
                "warning",
                f"Unresolved variable: {{{{ {var} }}}}",
                "manifest.template.yaml",
            )
            for var in unresolved
        )

    # Try to parse as valid YAML
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        errors.append(
            ValidationError("error", f"Resolved YAML is invalid: {e}", "manifest.template.yaml")
        )

    return errors


def validate_bundle(
    bundle_path: Path, context: dict[str, Any] | None = None
) -> list[ValidationError]:
    """Run all pre-install validations on a template bundle."""
    errors = []
    errors.extend(validate_skill_md(bundle_path))
    errors.extend(validate_setup_yaml(bundle_path))
    errors.extend(validate_template_resolves(bundle_path, context))
    return errors


def validate_post_install(
    manifest_path: Path,
    repo_root: Path | None = None,
) -> list[str]:
    """Run post-install validation (checks A-L) on a generated manifest.

    Returns list of failure/warning messages.
    """
    from robothor.templates.manifest_checks import validate_agent

    manifest = yaml.safe_load(manifest_path.read_text()) or {}

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    # Load all manifests for cross-reference checks
    manifest_dir = repo_root / "docs" / "agents"
    all_manifests = {}
    for f in sorted(manifest_dir.glob("*.yaml")):
        with f.open() as fh:
            data = yaml.safe_load(fh)
        if data and isinstance(data, dict) and "id" in data:
            all_manifests[data["id"]] = data

    # Try to get registered tools
    registered_tools: set[str] = set()
    try:
        from robothor.engine.tools import ToolRegistry

        registry = ToolRegistry()
        registered_tools = set(registry._schemas.keys())
    except Exception:
        pass

    results = validate_agent(manifest, all_manifests, registered_tools, repo_root=repo_root)
    messages = [
        f"[{r.check_id}] {r.name}: {r.status} -- {r.message}"
        for r in results
        if r.status in ("FAIL", "WARN")
    ]
    return messages


def validate_chain_post_install(
    manifest_path: Path,
    repo_root: Path | None = None,
) -> list[str]:
    """Run chain validation (checks M-R) on a generated manifest.

    Returns list of failure/warning messages.
    """
    from robothor.templates.chain_validator import validate_chain

    manifest = yaml.safe_load(manifest_path.read_text()) or {}

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    # Load all manifests for cross-reference checks
    manifest_dir = repo_root / "docs" / "agents"
    all_manifests = {}
    for f in sorted(manifest_dir.glob("*.yaml")):
        with f.open() as fh:
            data = yaml.safe_load(fh)
        if data and isinstance(data, dict) and "id" in data:
            all_manifests[data["id"]] = data

    results = validate_chain(manifest, all_manifests, repo_root=repo_root)
    messages = [
        f"[{r.check_id}] {r.name}: {r.status} -- {r.message}"
        for r in results
        if r.status in ("FAIL", "WARN")
    ]
    return messages
