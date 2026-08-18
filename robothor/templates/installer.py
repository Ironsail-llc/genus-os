"""
Agent template installer — install/remove/update orchestration.

The installer resolves template bundles into concrete manifests and instruction
files, then writes them to the locations the engine expects.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from robothor.templates.instance import InstanceConfig
from robothor.templates.resolver import TemplateResolver, deep_merge
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    default_workspace_root,
    safe_relative_path,
    trusted_directory,
    validate_identifier,
    validate_sha256,
    workspace_path,
)


def _find_repo_root() -> Path:
    """Find the workspace root that owns installed agent files.

    Never derived from ``__file__`` — in a wheel install that resolves inside
    site-packages, which would write ``docs/agents/`` and ``brain/`` files
    where the engine never looks. Delegates to the same ``ROBOTHOR_WORKSPACE``
    convention used across the rest of the engine.
    """
    return default_workspace_root()


def _find_defaults_path(repo_root: Path) -> Path | None:
    """Find _defaults.yaml in templates/agents/.

    Checked first directly under *repo_root* — the dev-checkout layout (and
    what test fixtures simulate), where the workspace and the platform's
    template catalog are the same tree. Falls back to the shared
    template-source resolver introduced in #245
    (``robothor.setup._find_template_dir``) for a real deployed instance,
    where the workspace (``ROBOTHOR_WORKSPACE``) and the package's bundled
    template catalog live in different directories — reusing that resolver
    rather than inventing a third resolution scheme.
    """
    candidates = [
        repo_root / "templates" / "agents" / "_defaults.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p

    from robothor.setup import _find_template_dir

    template_dir = _find_template_dir()
    if template_dir is not None:
        fallback = template_dir / "agents" / "_defaults.yaml"
        if fallback.exists():
            return fallback
    return None


def _bundle_file(bundle: Path, name: str, *, required: bool = False) -> Path | None:
    """Return a regular, non-symlink bundle control file."""

    path = contained_path(bundle, name, label=f"bundle {name} path")
    if not path.exists():
        if required:
            raise FileNotFoundError(f"{name} not found in {bundle}")
        return None
    if not path.is_file():
        raise TemplateSecurityError(f"Bundle {name} must be a regular file")
    return path


def _manifest_path(repo_root: Path, agent_id: str) -> Path:
    return workspace_path(
        repo_root,
        f"docs/agents/{agent_id}.yaml",
        allowed_prefix="docs/agents",
        label="agent manifest destination",
    )


def _instruction_path(repo_root: Path, value: object) -> Path:
    return workspace_path(
        repo_root,
        value,
        allowed_prefix="brain",
        label="agent instruction destination",
    )


def _load_mapping(path: Path, *, label: str) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise TemplateSecurityError(f"Invalid {label}: expected a YAML mapping")
    return data


def _owned_agent_files(repo_root: Path, agent_id: str) -> dict[str, Path]:
    """Derive removable files from the canonical manifest, never installed.yaml paths."""

    manifest_path = _manifest_path(repo_root, agent_id)
    files = {"manifest": manifest_path}
    if not manifest_path.exists():
        return files

    manifest = _load_mapping(manifest_path, label="installed agent manifest")
    manifest_id = validate_identifier(manifest.get("id"), label="manifest agent ID")
    if manifest_id != agent_id:
        raise TemplateSecurityError(
            f"Installed manifest ID {manifest_id!r} does not match requested agent {agent_id!r}"
        )
    instruction = manifest.get("instruction_file")
    if instruction:
        files["instruction"] = _instruction_path(repo_root, instruction)
    return files


def install(
    template_path: str | Path,
    overrides: dict[str, Any] | None = None,
    auto_yes: bool = False,
    instance_dir: Path | None = None,
    repo_root: Path | None = None,
    *,
    source: str = "local",
    source_ref: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Install an agent from a template bundle.

    1. Load setup.yaml from template
    2. Build variable context with resolution priority
    3. If not auto_yes: prompt for unresolved required variables
    4. Resolve manifest.template.yaml -> docs/agents/<id>.yaml
    5. Resolve instructions.template.md -> brain/<INSTRUCTION>.md
    6. Run manifest validation (checks A-L)
    7. Record in .robothor/installed.yaml

    Returns dict with install details.
    """
    if repo_root is None:
        repo_root = _find_repo_root()
    repo_root = repo_root.resolve(strict=True)

    if source not in {"local", "hub"}:
        raise TemplateSecurityError(f"Unsupported agent install source: {source}")
    if source == "hub":
        source_ref = validate_identifier(source_ref, label="hub bundle slug")
        source_sha256 = validate_sha256(source_sha256, label="hub bundle SHA-256")

    try:
        bundle = trusted_directory(template_path, label="template bundle")
    except TemplateSecurityError as error:
        raise FileNotFoundError(f"Template bundle not found or unsafe: {template_path}") from error

    # Load setup.yaml
    setup_path = _bundle_file(bundle, "setup.yaml", required=True)
    assert setup_path is not None
    setup = _load_mapping(setup_path, label="bundle setup.yaml")

    agent_id = validate_identifier(setup.get("agent_id", bundle.name), label="agent ID")
    if source == "hub" and agent_id != source_ref:
        raise TemplateSecurityError(
            "Downloaded bundle agent ID does not match its trusted registry slug"
        )
    version = setup.get("version", "0.0.0")

    # Check for existing agent (duplicate detection)
    manifest_dest = _manifest_path(repo_root, agent_id)
    if manifest_dest.exists() and not auto_yes:
        overwrite = (
            input(f"  Agent '{agent_id}' already installed. Overwrite? [y/N]: ").strip().lower()
        )
        if overwrite != "y":
            print(f"  Skipping {agent_id}")
            return {"agent_id": agent_id, "skipped": True, "reason": "already installed"}
        # In auto_yes mode, overwrite silently

    # Instance config
    instance = InstanceConfig.load(instance_dir)
    instance_config = instance.config
    agent_overrides = instance.get_agent_overrides(agent_id)

    # Load global defaults
    defaults_path = _find_defaults_path(repo_root)
    defaults = None
    if defaults_path:
        defaults = yaml.safe_load(defaults_path.read_text()) or {}

    # Build context
    resolver = TemplateResolver()
    context = resolver.build_context(
        setup_yaml=setup,
        defaults_yaml=defaults,
        instance_config=instance_config,
        overrides=deep_merge(agent_overrides, overrides or {}),
    )

    # Prompt for unresolved required variables (interactive mode)
    if not auto_yes:
        variables = setup.get("variables", {})
        for var_name, var_def in variables.items():
            if not isinstance(var_def, dict):
                continue
            if var_def.get("required") and var_name not in context:
                prompt_text = var_def.get("prompt", f"Enter value for {var_name}")
                default_hint = f" [{var_def.get('default', '')}]" if "default" in var_def else ""
                value = input(f"  {prompt_text}{default_hint}: ").strip()
                if value:
                    context[var_name] = value
                elif "default" in var_def:
                    context[var_name] = var_def["default"]

    # Resolve template files
    output_files: dict[str, tuple[Path, str]] = {}
    manifest_data: dict[str, Any] = {}

    manifest_template = _bundle_file(bundle, "manifest.template.yaml", required=True)
    if manifest_template is not None:
        manifest_content = resolver.resolve_file(manifest_template, context, trusted_root=bundle)
        loaded_manifest = yaml.safe_load(manifest_content) or {}
        if not isinstance(loaded_manifest, dict):
            raise TemplateSecurityError("Invalid resolved manifest: expected a YAML mapping")
        manifest_data = loaded_manifest
        manifest_id = validate_identifier(manifest_data.get("id"), label="manifest agent ID")
        if manifest_id != agent_id:
            raise TemplateSecurityError(
                f"Manifest ID {manifest_id!r} does not match setup agent ID {agent_id!r}"
            )
        manifest_dest = _manifest_path(repo_root, manifest_id)
        output_files["manifest"] = (manifest_dest, manifest_content)

    setup_instruction = setup.get("instruction_file_path")
    manifest_instruction = manifest_data.get("instruction_file")
    if setup_instruction:
        _instruction_path(repo_root, setup_instruction)
    if manifest_instruction:
        _instruction_path(repo_root, manifest_instruction)
    if (
        setup_instruction
        and manifest_instruction
        and safe_relative_path(setup_instruction, label="setup instruction path")
        != safe_relative_path(manifest_instruction, label="manifest instruction path")
    ):
        raise TemplateSecurityError(
            "setup.yaml and the resolved manifest declare different instruction paths"
        )

    instructions_template = _bundle_file(bundle, "instructions.template.md")
    if instructions_template is not None:
        instructions_content = resolver.resolve_file(
            instructions_template, context, trusted_root=bundle
        )
        # Determine instruction file destination from resolved manifest
        instr_path = setup_instruction or manifest_instruction
        if instr_path:
            instr_dest = _instruction_path(repo_root, instr_path)
            output_files["instruction"] = (instr_dest, instructions_content)

    # Write files atomically — temp files first, then validate, then move
    temp_files: dict[str, tuple[Path, Path]] = {}  # key -> (temp_path, final_path)
    validation_messages = []
    chain_validation_messages = []

    try:
        for key, (dest, content) in output_files.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write to temp file in same directory (ensures same filesystem for rename)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=f".{dest.stem}_", dir=dest.parent)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            temp_files[key] = (Path(tmp_path), dest)

        # Run post-install validation on temp files
        if "manifest" in temp_files:
            from robothor.templates.validators import validate_post_install

            tmp_manifest = temp_files["manifest"][0]
            validation_messages = validate_post_install(tmp_manifest, repo_root)

            # Surface chain validation errors as warnings
            try:
                from robothor.templates.validators import validate_chain_post_install

                chain_validation_messages = validate_chain_post_install(tmp_manifest, repo_root)
            except Exception as e:
                chain_validation_messages = [f"Chain validation warning: {e}"]

        # All validation passed — move temp files to final paths
        for temp_p, final_p in temp_files.values():
            temp_p.rename(final_p)

    except Exception:
        # Cleanup temp files on any failure
        for temp_p, _ in temp_files.values():
            if temp_p.exists():
                temp_p.unlink()
        raise

    # Record installation
    recorded_source = str(bundle)
    if source == "hub":
        assert source_ref is not None  # validated before any bundle files were read
        recorded_source = source_ref
    instance.record_install(
        agent_id=agent_id,
        source=source,
        source_path=recorded_source,
        version=version,
        variables=context,
        manifest_path=output_files["manifest"][0].relative_to(repo_root).as_posix(),
        instruction_path=(
            output_files["instruction"][0].relative_to(repo_root).as_posix()
            if "instruction" in output_files
            else ""
        ),
        source_sha256=source_sha256,
    )

    return {
        "agent_id": agent_id,
        "version": version,
        "files": {k: str(v[0]) for k, v in output_files.items()},
        "validation": validation_messages,
        "chain_validation": chain_validation_messages,
        "context": context,
        "source": source,
    }


def remove(
    agent_id: str,
    archive: bool = False,
    instance_dir: Path | None = None,
    repo_root: Path | None = None,
) -> bool:
    """Remove an installed agent.

    1. Read .robothor/installed.yaml for file paths
    2. Delete manifest + instruction file (or archive)
    3. Remove entry from installed.yaml

    Returns True if agent was found and removed.
    """
    if repo_root is None:
        repo_root = _find_repo_root()
    repo_root = repo_root.resolve(strict=True)
    agent_id = validate_identifier(agent_id, label="agent ID")

    instance = InstanceConfig.load(instance_dir)
    agents = instance.installed_agents

    if agent_id not in agents:
        return False

    # installed.yaml is mutable state, not an authorization source for deletion.
    # Derive the only removable paths from the strict ID and canonical manifest.
    file_paths = _owned_agent_files(repo_root, agent_id)

    # Archive or delete
    if archive:
        instance.archive_agent(agent_id, file_paths)

    for path in file_paths.values():
        if path.is_symlink():
            raise TemplateSecurityError(f"Refusing to remove symlink: {path}")
        if path.is_file():
            path.unlink()

    instance.record_remove(agent_id)
    return True


def update(
    agent_id: str,
    new_template_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    auto_yes: bool = False,
    instance_dir: Path | None = None,
    repo_root: Path | None = None,
    *,
    source: str = "local",
    source_ref: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Update an installed agent with a new or same template.

    1. Read current install record from .robothor/installed.yaml
    2. Load an explicit template or a same-ID bundle from the local catalog
    3. Re-resolve with existing variables + any new defaults
    4. Diff against current manifest -> show changes
    5. Write updated files
    6. Update installed.yaml

    Returns install result dict, or None if agent not found.
    """
    if repo_root is None:
        repo_root = _find_repo_root()
    repo_root = repo_root.resolve(strict=True)
    agent_id = validate_identifier(agent_id, label="agent ID")

    instance = InstanceConfig.load(instance_dir)
    agents = instance.installed_agents

    if agent_id not in agents:
        return None

    record = agents[agent_id]

    # Never execute a source path recovered from mutable installed.yaml state.
    # Without an explicit source, only a deterministic in-workspace catalog
    # bundle with the same strict ID is eligible for a local update.
    template_path = new_template_path
    if template_path is None:
        catalog_root = workspace_path(
            repo_root,
            "templates/agents",
            allowed_prefix="templates/agents",
            label="local template catalog",
        )
        if catalog_root.is_dir():
            for department in catalog_root.iterdir():
                if not department.is_dir() or department.is_symlink():
                    continue
                candidate = department / agent_id
                if candidate.is_dir() and not candidate.is_symlink():
                    template_path = candidate
                    break
        if template_path is None:
            raise TemplateSecurityError(
                "No trusted local template found; provide an explicit update template"
            )

    # Merge existing variables with new overrides
    existing_vars = record.get("variables", {})
    merged_overrides = deep_merge(existing_vars, overrides or {})

    # Read current files for diff
    current_files = {}
    for file_type, path in _owned_agent_files(repo_root, agent_id).items():
        if path.is_file():
            current_files[file_type] = path.read_text()

    # Install with merged overrides
    result = install(
        template_path=template_path,
        overrides=merged_overrides,
        auto_yes=auto_yes,
        instance_dir=instance_dir,
        repo_root=repo_root,
        source=source,
        source_ref=source_ref,
        source_sha256=source_sha256,
    )

    # Generate diffs for user review
    diffs = {}
    for file_type, old_content in current_files.items():
        new_path = result.get("files", {}).get(file_type)
        if new_path:
            p = Path(new_path)
            if p.exists():
                new_content = p.read_text()
                if old_content != new_content:
                    diff = difflib.unified_diff(
                        old_content.splitlines(keepends=True),
                        new_content.splitlines(keepends=True),
                        fromfile=f"old/{file_type}",
                        tofile=f"new/{file_type}",
                    )
                    diffs[file_type] = "".join(diff)

    result["diffs"] = diffs
    return result


def import_agent(
    agent_id: str,
    output_dir: str | Path | None = None,
    repo_root: Path | None = None,
    defaults_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reverse-engineer an existing agent manifest into a template bundle.

    1. Read manifest from docs/agents/<id>.yaml
    2. Read instruction file from manifest's instruction_file field
    3. Compare against _defaults.yaml -> identify deviations
    4. Generate manifest.template.yaml with {{ variable }} placeholders
    5. Generate instructions.template.md (usually unchanged)
    6. Generate setup.yaml with deviations as variable defaults
    7. Generate SKILL.md from manifest metadata
    8. Write bundle to output_dir

    Returns dict with generated file paths.
    """
    if repo_root is None:
        repo_root = _find_repo_root()
    repo_root = repo_root.resolve(strict=True)
    agent_id = validate_identifier(agent_id, label="agent ID")

    # Load manifest
    manifest_path = _manifest_path(repo_root, agent_id)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = _load_mapping(manifest_path, label="agent manifest")
    manifest_id = validate_identifier(manifest.get("id"), label="manifest agent ID")
    if manifest_id != agent_id:
        raise TemplateSecurityError(
            f"Manifest ID {manifest_id!r} does not match requested agent {agent_id!r}"
        )
    declared_instruction = manifest.get("instruction_file")
    if declared_instruction:
        _instruction_path(repo_root, declared_instruction)

    # Load defaults for comparison
    defaults: dict[str, Any] = {}
    if defaults_path is None:
        defaults_path = _find_defaults_path(repo_root)
    if defaults_path:
        dp = Path(defaults_path)
        if dp.exists():
            defaults = yaml.safe_load(dp.read_text()) or {}

    # Determine output directory
    department = validate_identifier(manifest.get("department", "custom"), label="department")
    out_path: Path
    if output_dir is None:
        out_path = workspace_path(
            repo_root,
            f"templates/agents/{department}/{agent_id}",
            allowed_prefix="templates/agents",
            label="template output directory",
        )
    else:
        out_path = Path(output_dir).expanduser().resolve(strict=False)
    out_path.mkdir(parents=True, exist_ok=True)

    def output_file(name: str) -> Path:
        return contained_path(out_path, name, label="generated template path")

    # Identify instance-specific variables (deviations from defaults)
    variables = {}
    variable_map = {}  # Maps manifest values to {{ variable }} names

    # Model
    model_primary = manifest.get("model", {}).get("primary", "")
    default_model = defaults.get("model_primary", "")
    if model_primary and model_primary != default_model:
        variables["model_primary"] = {
            "type": "string",
            "default": model_primary,
            "description": "Primary LLM model",
        }
        variable_map[model_primary] = "model_primary"
    elif model_primary:
        variable_map[model_primary] = "model_primary"

    # Timezone
    tz = manifest.get("schedule", {}).get("timezone", "")
    default_tz = defaults.get("timezone", "")
    if tz and tz != default_tz:
        variables["timezone"] = {
            "type": "string",
            "default": tz,
            "description": "Schedule timezone",
        }

    # Cron
    cron = manifest.get("schedule", {}).get("cron", "")
    if cron:
        variables["cron_expr"] = {
            "type": "string",
            "default": cron,
            "description": "Cron schedule expression",
        }

    # Delivery
    delivery_mode = manifest.get("delivery", {}).get("mode", "none")
    if delivery_mode != defaults.get("delivery_mode", "none"):
        variables["delivery_mode"] = {
            "type": "string",
            "default": delivery_mode,
            "description": "Delivery mode",
        }

    # Reports to
    reports_to = manifest.get("reports_to")
    if reports_to and reports_to != defaults.get("reports_to"):
        variables["reports_to"] = {
            "type": "string",
            "default": reports_to,
            "description": "Agent to report to",
        }

    # Build manifest template — replace instance-specific values with {{ variable }}
    manifest_content = manifest_path.read_text()
    template_content = manifest_content

    # Version is always templatized
    version_str = manifest.get("version", "")
    if version_str:
        template_content = template_content.replace(
            f'version: "{version_str}"',
            'version: "{{ version }}"',
        )

    # Replace values in order of specificity to avoid partial matches
    escalates_to = manifest.get("escalates_to")
    replacements = [
        (f"primary: {model_primary}", "primary: {{ model_primary }}"),
        (f"timezone: {tz}", "timezone: {{ timezone }}"),
    ]

    # Cron needs special handling for quoted values
    if cron:
        replacements.append((f'cron: "{cron}"', 'cron: "{{ cron_expr }}"'))

    # Delivery mode (only if non-default)
    if delivery_mode and delivery_mode != "none":
        replacements.append((f"mode: {delivery_mode}", "mode: {{ delivery_mode }}"))

    # reports_to and escalates_to
    if reports_to:
        replacements.append((f"reports_to: {reports_to}", "reports_to: {{ reports_to }}"))
    if escalates_to:
        replacements.append((f"escalates_to: {escalates_to}", "escalates_to: {{ escalates_to }}"))

    for old, new in replacements:
        if old in template_content:
            template_content = template_content.replace(old, new, 1)  # Only first occurrence

    # Write manifest.template.yaml
    output_file("manifest.template.yaml").write_text(template_content)

    # Copy instruction file as template
    instr_file = manifest.get("instruction_file")
    if instr_file:
        instr_path = _instruction_path(repo_root, instr_file)
        if instr_path.exists():
            output_file("instructions.template.md").write_text(instr_path.read_text())

    # Generate setup.yaml
    setup = {
        "agent_id": agent_id,
        "version": manifest.get("version", "0.0.0"),
        "instruction_file_path": instr_file or "",
        "variables": variables,
    }
    output_file("setup.yaml").write_text(
        yaml.dump(setup, default_flow_style=False, sort_keys=False)
    )

    # Generate SKILL.md using description optimizer
    instr_content = ""
    if instr_file:
        instr_path = _instruction_path(repo_root, instr_file)
        if instr_path.exists():
            instr_content = instr_path.read_text()

    try:
        from robothor.templates.description_optimizer import generate_skill_md

        skill_content = generate_skill_md(manifest, instr_content)
    except Exception:
        # Fallback to basic SKILL.md
        skill_content = f"""---
name: {manifest.get("name", agent_id)}
version: {manifest.get("version", "0.0.0")}
description: {manifest.get("description", "")}
format: robothor-native/v1
department: {department}
---

# {manifest.get("name", agent_id)}

{manifest.get("description", "")}
"""

    output_file("SKILL.md").write_text(skill_content)

    # Generate programmatic.json
    import json

    programmatic = {
        "name": manifest.get("name", agent_id),
        "id": agent_id,
        "version": manifest.get("version", "0.0.0"),
        "format": "robothor-native/v1",
        "department": department,
        "description": manifest.get("description", ""),
        "tags": manifest.get("tags_produced", []),
    }
    output_file("programmatic.json").write_text(json.dumps(programmatic, indent=2) + "\n")

    # Register in installed.yaml
    instance = InstanceConfig.load()
    instance.record_install(
        agent_id=agent_id,
        source="local",
        source_path=str(out_path),
        version=manifest.get("version", "0.0.0"),
        variables={
            k: v.get("default", "") if isinstance(v, dict) else v for k, v in variables.items()
        },
        manifest_path=manifest_path.relative_to(repo_root).as_posix(),
        instruction_path=safe_relative_path(instr_file).as_posix() if instr_file else "",
    )

    # Score hub readiness
    hub_readiness_score = 0
    try:
        from robothor.templates.description_optimizer import score_hub_readiness

        report = score_hub_readiness(out_path)
        hub_readiness_score = report.score
    except Exception:
        pass

    return {
        "agent_id": agent_id,
        "output_dir": str(out_path),
        "files": [
            str(out_path / "manifest.template.yaml"),
            str(out_path / "instructions.template.md"),
            str(out_path / "setup.yaml"),
            str(out_path / "SKILL.md"),
            str(out_path / "programmatic.json"),
        ],
        "variables": variables,
        "hub_readiness_score": hub_readiness_score,
    }
