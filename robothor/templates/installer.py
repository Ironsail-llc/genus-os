"""
Agent template installer — install/remove/update orchestration.

The installer resolves template bundles into concrete manifests and instruction
files, then writes them to the locations the engine expects.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import yaml

from robothor.templates.instance import InstanceConfig
from robothor.templates.resolver import TemplateResolver, deep_merge


def _find_repo_root() -> Path:
    """Find the repository root."""
    return Path(__file__).resolve().parent.parent.parent


def _find_defaults_path(repo_root: Path) -> Path | None:
    """Find _defaults.yaml in templates/agents/."""
    candidates = [
        repo_root / "templates" / "agents" / "_defaults.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def install(
    template_path: str | Path,
    overrides: dict[str, Any] | None = None,
    auto_yes: bool = False,
    instance_dir: Path | None = None,
    repo_root: Path | None = None,
) -> dict:
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

    bundle = Path(template_path)
    if not bundle.is_dir():
        raise FileNotFoundError(f"Template bundle not found: {bundle}")

    # Load setup.yaml
    setup_path = bundle / "setup.yaml"
    if not setup_path.exists():
        raise FileNotFoundError(f"setup.yaml not found in {bundle}")
    setup = yaml.safe_load(setup_path.read_text()) or {}

    agent_id = setup.get("agent_id", bundle.name)
    version = setup.get("version", "0.0.0")

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
    output_files = {}

    manifest_template = bundle / "manifest.template.yaml"
    if manifest_template.exists():
        manifest_content = resolver.resolve_file(manifest_template, context)
        manifest_data = yaml.safe_load(manifest_content) or {}
        manifest_id = manifest_data.get("id", agent_id)
        manifest_dest = repo_root / "docs" / "agents" / f"{manifest_id}.yaml"
        output_files["manifest"] = (manifest_dest, manifest_content)

    instructions_template = bundle / "instructions.template.md"
    if instructions_template.exists():
        instructions_content = resolver.resolve_file(instructions_template, context)
        # Determine instruction file destination from resolved manifest
        instr_path = setup.get("instruction_file_path")
        if not instr_path and manifest_data:
            instr_path = manifest_data.get("instruction_file", "")
        if instr_path:
            instr_dest = repo_root / instr_path
            output_files["instruction"] = (instr_dest, instructions_content)

    # Write files
    for _file_type, (dest, content) in output_files.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    # Run post-install validation
    validation_messages = []
    if "manifest" in output_files:
        from robothor.templates.validators import validate_post_install

        manifest_dest = output_files["manifest"][0]
        validation_messages = validate_post_install(manifest_dest, repo_root)

    # Record installation
    instance.record_install(
        agent_id=agent_id,
        source="local",
        source_path=str(bundle),
        version=version,
        variables=context,
        manifest_path=str(output_files.get("manifest", ("",))[0]),
        instruction_path=str(output_files.get("instruction", ("",))[0]),
    )

    return {
        "agent_id": agent_id,
        "version": version,
        "files": {k: str(v[0]) for k, v in output_files.items()},
        "validation": validation_messages,
        "context": context,
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

    instance = InstanceConfig.load(instance_dir)
    agents = instance.installed_agents

    if agent_id not in agents:
        return False

    record = agents[agent_id]
    files = record.get("files", {})

    file_paths = {}
    for file_type, path_str in files.items():
        if path_str:
            p = Path(path_str)
            if not p.is_absolute():
                p = repo_root / p
            file_paths[file_type] = p

    # Archive or delete
    if archive:
        instance.archive_agent(agent_id, file_paths)

    for _file_type, path in file_paths.items():
        if path.exists():
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
) -> dict | None:
    """Update an installed agent with a new or same template.

    1. Read current install record from .robothor/installed.yaml
    2. Load new template (from path or same source)
    3. Re-resolve with existing variables + any new defaults
    4. Diff against current manifest -> show changes
    5. Write updated files
    6. Update installed.yaml

    Returns install result dict, or None if agent not found.
    """
    if repo_root is None:
        repo_root = _find_repo_root()

    instance = InstanceConfig.load(instance_dir)
    agents = instance.installed_agents

    if agent_id not in agents:
        return None

    record = agents[agent_id]

    # Determine template path
    template_path = new_template_path or record.get("source_path")
    if not template_path:
        return None

    # Merge existing variables with new overrides
    existing_vars = record.get("variables", {})
    merged_overrides = deep_merge(existing_vars, overrides or {})

    # Read current files for diff
    current_files = {}
    for file_type, path_str in record.get("files", {}).items():
        if path_str:
            p = Path(path_str)
            if not p.is_absolute():
                p = repo_root / p
            if p.exists():
                current_files[file_type] = p.read_text()

    # Install with merged overrides
    result = install(
        template_path=template_path,
        overrides=merged_overrides,
        auto_yes=auto_yes,
        instance_dir=instance_dir,
        repo_root=repo_root,
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
) -> dict:
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

    # Load manifest
    manifest_path = repo_root / "docs" / "agents" / f"{agent_id}.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = yaml.safe_load(manifest_path.read_text()) or {}

    # Load defaults for comparison
    defaults = {}
    if defaults_path is None:
        defaults_path = _find_defaults_path(repo_root)
    if defaults_path:
        dp = Path(defaults_path)
        if dp.exists():
            defaults = yaml.safe_load(dp.read_text()) or {}

    # Determine output directory
    department = manifest.get("department", "custom")
    if output_dir is None:
        output_dir = repo_root / "templates" / "agents" / department / agent_id
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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

    # Build manifest template — replace instance values with {{ variable }}
    manifest_content = manifest_path.read_text()
    template_content = manifest_content

    # Replace model_primary with template variable
    if model_primary:
        template_content = template_content.replace(
            f"primary: {model_primary}",
            "primary: {{ model_primary }}",
        )

    # Replace timezone
    if tz:
        template_content = template_content.replace(
            f"timezone: {tz}",
            "timezone: {{ timezone }}",
        )

    # Replace cron
    if cron:
        template_content = template_content.replace(
            f'cron: "{cron}"',
            'cron: "{{ cron_expr }}"',
        )

    # Replace delivery mode
    if delivery_mode != "none":
        template_content = template_content.replace(
            f"mode: {delivery_mode}",
            "mode: {{ delivery_mode }}",
        )

    # Replace reports_to
    if reports_to:
        template_content = template_content.replace(
            f"reports_to: {reports_to}",
            "reports_to: {{ reports_to }}",
        )

    # Replace escalates_to
    escalates_to = manifest.get("escalates_to")
    if escalates_to:
        template_content = template_content.replace(
            f"escalates_to: {escalates_to}",
            "escalates_to: {{ escalates_to }}",
        )

    # Write manifest.template.yaml
    (output_dir / "manifest.template.yaml").write_text(template_content)

    # Copy instruction file as template
    instr_file = manifest.get("instruction_file")
    if instr_file:
        instr_path = repo_root / instr_file
        if instr_path.exists():
            (output_dir / "instructions.template.md").write_text(instr_path.read_text())

    # Generate setup.yaml
    setup = {
        "agent_id": agent_id,
        "version": manifest.get("version", "0.0.0"),
        "instruction_file_path": instr_file or "",
        "variables": variables,
    }
    (output_dir / "setup.yaml").write_text(
        yaml.dump(setup, default_flow_style=False, sort_keys=False)
    )

    # Generate SKILL.md
    skill_content = f"""---
name: {manifest.get("name", agent_id)}
version: {manifest.get("version", "0.0.0")}
description: {manifest.get("description", "")}
format: robothor-native/v1
department: {department}
---

# {manifest.get("name", agent_id)}

{manifest.get("description", "")}

## Variables

"""
    for var_name, var_def in variables.items():
        if isinstance(var_def, dict):
            skill_content += f"- **{var_name}**: {var_def.get('description', '')} (default: `{var_def.get('default', '')}`)\n"

    (output_dir / "SKILL.md").write_text(skill_content)

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
    (output_dir / "programmatic.json").write_text(json.dumps(programmatic, indent=2) + "\n")

    # Register in installed.yaml
    instance = InstanceConfig.load()
    instance.record_install(
        agent_id=agent_id,
        source="local",
        source_path=str(output_dir),
        version=manifest.get("version", "0.0.0"),
        variables={
            k: v.get("default", "") if isinstance(v, dict) else v for k, v in variables.items()
        },
        manifest_path=str(manifest_path),
        instruction_path=str(repo_root / instr_file) if instr_file else "",
    )

    return {
        "agent_id": agent_id,
        "output_dir": str(output_dir),
        "files": [
            str(output_dir / "manifest.template.yaml"),
            str(output_dir / "instructions.template.md"),
            str(output_dir / "setup.yaml"),
            str(output_dir / "SKILL.md"),
            str(output_dir / "programmatic.json"),
        ],
        "variables": variables,
    }
