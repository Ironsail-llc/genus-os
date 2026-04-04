"""Agent management commands (scaffold, install, remove, template hub, etc.)."""

from __future__ import annotations

import argparse  # noqa: TC003
from pathlib import Path
from typing import Any


def _load_manifest(path: str | Path) -> tuple[Any, Any]:
    """Load a YAML manifest, preferring ruamel.yaml for comment preservation."""
    try:
        from ruamel.yaml import YAML

        yaml_handler = YAML()
        yaml_handler.preserve_quotes = True
        with Path(path).open() as f:
            data = yaml_handler.load(f)
        return data, yaml_handler
    except ImportError:
        import yaml

        print("Note: ruamel.yaml not installed — comments may not be preserved")
        with Path(path).open() as f:
            return yaml.safe_load(f), None


def _save_manifest(path: str | Path, data: Any, yaml_handler: Any = None) -> None:
    """Save a YAML manifest using the same handler that loaded it."""
    if yaml_handler is not None:
        with Path(path).open("w") as f:
            yaml_handler.dump(data, f)
    else:
        import yaml

        with Path(path).open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def cmd_agent(args: argparse.Namespace) -> int:
    sub = getattr(args, "agent_command", None)
    if sub == "scaffold":
        return _cmd_agent_scaffold(args)
    if sub == "list":
        return _cmd_agent_list()
    if sub == "catalog":
        return _cmd_agent_catalog(args)
    if sub == "install":
        return _cmd_agent_install(args)
    if sub == "remove":
        return _cmd_agent_remove(args)
    if sub == "update":
        return _cmd_agent_update(args)
    if sub == "resolve":
        return _cmd_agent_resolve(args)
    if sub == "import":
        return _cmd_agent_import(args)
    if sub == "setup":
        return _cmd_agent_setup()
    if sub == "search":
        return _cmd_agent_search(args)
    if sub == "publish":
        return _cmd_agent_publish(args)
    if sub == "bind":
        return _cmd_agent_bind(args)
    if sub == "unbind":
        return _cmd_agent_unbind(args)
    print(
        "Usage: robothor agent {scaffold|list|catalog|install|remove|update|resolve|import|setup|search|publish|bind|unbind}"
    )
    return 0


def _cmd_agent_scaffold(args: argparse.Namespace) -> int:
    """Scaffold a new agent — create manifest + instruction file from templates."""
    import re
    from datetime import UTC, datetime

    agent_id = args.agent_id
    description = args.description or f"A new agent: {agent_id}"

    # Validate kebab-case
    if not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", agent_id):
        print(f"Error: agent_id must be kebab-case (e.g., 'ticket-router'), got: {agent_id}")
        return 1

    # Derive names
    agent_name = agent_id.replace("-", " ").title()
    instruction_filename = agent_id.upper().replace("-", "_") + ".md"
    version = datetime.now(UTC).strftime("%Y-%m-%d")
    status_file = f"brain/memory/{agent_id}-status.md"

    # Paths
    workspace = Path.home() / "robothor"
    manifest_dir = workspace / "docs" / "agents"
    brain_dir = workspace / "brain"
    template_dir = workspace / "templates"

    manifest_path = manifest_dir / f"{agent_id}.yaml"
    instruction_path = brain_dir / instruction_filename

    # Check for conflicts
    if manifest_path.exists():
        print(f"Error: Manifest already exists: {manifest_path}")
        return 1
    if instruction_path.exists():
        print(f"Error: Instruction file already exists: {instruction_path}")
        return 1

    # Load templates
    manifest_template = template_dir / "agent-manifest.yaml"
    instruction_template = template_dir / "agent-instructions.md"

    if not manifest_template.exists():
        print(f"Error: Template not found: {manifest_template}")
        return 1
    if not instruction_template.exists():
        print(f"Error: Template not found: {instruction_template}")
        return 1

    replacements = {
        "{AGENT_ID}": agent_id,
        "{AGENT_NAME}": agent_name,
        "{DESCRIPTION}": description,
        "{VERSION}": version,
        "{INSTRUCTION_FILENAME}": instruction_filename,
        "{STATUS_FILE}": status_file,
    }

    # Write manifest
    manifest_content = manifest_template.read_text()
    for placeholder, value in replacements.items():
        manifest_content = manifest_content.replace(placeholder, value)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_content)

    # Write instruction file
    instruction_content = instruction_template.read_text()
    for placeholder, value in replacements.items():
        instruction_content = instruction_content.replace(placeholder, value)
    brain_dir.mkdir(parents=True, exist_ok=True)
    instruction_path.write_text(instruction_content)

    print(f"Scaffolded agent: {agent_name} ({agent_id})")
    print()
    print(f"  Manifest:     {manifest_path}")
    print(f"  Instructions: {instruction_path}")
    print()
    print("Next steps:")
    print(f"  1. Edit the manifest:     {manifest_path}")
    print(f"  2. Edit the instructions: {instruction_path}")
    print(f"  3. Validate:              python scripts/validate_agents.py --agent {agent_id}")
    print("  4. Restart engine:        sudo systemctl restart robothor-engine")
    return 0


def _cmd_agent_list() -> int:
    """List installed agents with source, version, and install date."""
    from robothor.templates.instance import InstanceConfig

    instance = InstanceConfig.load()
    agents = instance.installed_agents

    if not agents:
        print("No agents installed via template system.")
        print("Use 'robothor agent install' or 'robothor agent import' to get started.")
        return 0

    print(f"{'Agent ID':<25} {'Version':<12} {'Source':<10} {'Installed'}")
    print("-" * 70)
    for agent_id, info in sorted(agents.items()):
        version = info.get("version", "?")
        source = info.get("source", "?")
        installed = str(info.get("installed_at", ""))[:10]
        print(f"{agent_id:<25} {version:<12} {source:<10} {installed}")

    print(f"\n{len(agents)} agent(s) installed")
    return 0


def _cmd_agent_catalog(args: argparse.Namespace) -> int:
    """Browse available templates by department or preset."""
    from robothor.templates.catalog import Catalog

    catalog = Catalog()
    department_filter = getattr(args, "department", None)

    if department_filter:
        agents = catalog.get_department_agents(department_filter)
        dept = catalog.departments.get(department_filter, {})
        if not agents:
            print(f"Unknown department: {department_filter}")
            print(f"Available: {', '.join(catalog.departments.keys())}")
            return 1
        print(f"Department: {dept.get('name', department_filter)}")
        print(f"  {dept.get('description', '')}")
        print()
        for a in agents:
            print(f"  - {a}")
        return 0

    # Show full catalog
    print("=== Agent Template Catalog ===\n")

    print("Departments:")
    for dept in catalog.list_departments():
        print(f"  {dept['id']:<20} {dept['name']:<25} ({len(dept['agents'])} agents)")
        print(f"  {'':20} {dept['description']}")
    print()

    print("Presets:")
    for preset in catalog.list_presets():
        count = len(preset["agents"])
        print(f"  {preset['id']:<20} {preset['description']:<40} ({count} agents)")
    print()

    # Show available templates on disk
    templates = catalog.list_available_templates()
    if templates:
        print(f"Templates on disk: {len(templates)}")
        for t in templates:
            print(f"  {t['id']:<25} dept={t['department']:<15} v{t['version']}")
    else:
        print("No template bundles found on disk.")
        print("  Import existing: robothor agent import <id>")
    return 0


def _cmd_agent_install(args: argparse.Namespace) -> int:
    """Install an agent from a template bundle or preset."""
    from robothor.templates.catalog import Catalog
    from robothor.templates.installer import install

    auto_yes = getattr(args, "yes", False)
    preset = getattr(args, "preset", None)

    # Parse --set key=value overrides
    cli_overrides = {}
    for item in getattr(args, "set", []) or []:
        if "=" in item:
            k, _, v = item.partition("=")
            cli_overrides[k.strip()] = v.strip()

    # Preset mode: install multiple agents
    if preset:
        catalog = Catalog()
        agents = catalog.get_preset_agents(preset)
        if not agents:
            print(f"Unknown preset: {preset}")
            print(f"Available: {', '.join(catalog.presets.keys())}")
            return 1

        print(f"Installing preset '{preset}': {len(agents)} agents")
        installed = 0
        for agent_id in agents:
            template_path = catalog.find_template(agent_id)
            if not template_path:
                print(f"  {agent_id}: template not found, skipping")
                continue
            try:
                result = install(str(template_path), overrides=cli_overrides, auto_yes=auto_yes)
                print(f"  {agent_id}: installed (v{result['version']})")
                installed += 1
            except Exception as e:
                print(f"  {agent_id}: FAILED -- {e}")
        print(f"\n{installed}/{len(agents)} agents installed")
        return 0

    # Single agent mode
    source = args.source
    source_path = Path(source)

    # If source is an agent ID (not a path), try to find its template
    if not source_path.is_dir():
        catalog = Catalog()
        template_path = catalog.find_template(source)
        if template_path:
            source_path = template_path
        else:
            try:
                from robothor.templates.hub_client import HubClient

                print(f"Template '{source}' not found locally. Searching hub...")
                with HubClient() as hub:
                    bundle = hub.get_bundle(source)
                    if bundle:
                        print(f"Found on hub: {bundle.get('name', source)}")
                        extracted = hub.download_bundle(source)
                        source_path = extracted
                    else:
                        print(f"Template not found: {source}")
                        return 1
            except Exception as e:
                print(f"Template not found locally, hub lookup failed: {e}")
                return 1

    try:
        result = install(str(source_path), overrides=cli_overrides, auto_yes=auto_yes)
    except Exception as e:
        print(f"Error: {e}")
        return 1

    print(f"Installed: {result['agent_id']} (v{result['version']})")
    for file_type, path in result.get("files", {}).items():
        print(f"  {file_type}: {path}")

    validation = result.get("validation", [])
    if validation:
        print("\nValidation warnings:")
        for msg in validation:
            print(f"  {msg}")

    print("\nNext steps:")
    print("  python scripts/validate_agents.py --agent", result["agent_id"])
    print("  sudo systemctl restart robothor-engine")
    return 0


def _cmd_agent_remove(args: argparse.Namespace) -> int:
    """Remove an installed agent."""
    from robothor.templates.installer import remove

    agent_id = args.agent_id
    archive = getattr(args, "archive", False)

    if remove(agent_id, archive=archive):
        action = "Archived" if archive else "Removed"
        print(f"{action}: {agent_id}")
        print("Restart engine: sudo systemctl restart robothor-engine")
        return 0
    print(f"Agent not found in installed registry: {agent_id}")
    print("Use 'robothor agent list' to see installed agents.")
    return 1


def _cmd_agent_update(args: argparse.Namespace) -> int:
    """Update an installed agent from its template."""
    from robothor.templates.installer import update
    from robothor.templates.instance import InstanceConfig

    agent_id = getattr(args, "agent_id", None)
    template_path = getattr(args, "template", None)

    if not agent_id:
        # Update all installed agents
        instance = InstanceConfig.load()
        agents = instance.installed_agents
        if not agents:
            print("No agents installed.")
            return 0
        updated = 0
        for aid in agents:
            result = update(aid)
            if result:
                diffs = result.get("diffs", {})
                if diffs:
                    print(f"  {aid}: updated")
                    updated += 1
                else:
                    print(f"  {aid}: up to date")
        print(f"\n{updated} agent(s) updated")
        return 0

    result = update(agent_id, new_template_path=template_path)
    if result is None:
        print(f"Agent not found: {agent_id}")
        return 1

    diffs = result.get("diffs", {})
    if diffs:
        print(f"Updated: {agent_id}")
        for file_type, diff_text in diffs.items():
            print(f"\n--- {file_type} changes ---")
            print(diff_text)
    else:
        print(f"{agent_id}: already up to date")
    return 0


def _cmd_agent_resolve(args: argparse.Namespace) -> int:
    """Preview variable resolution without writing files."""
    from robothor.templates.resolver import TemplateResolver

    path = args.path

    # Parse --set overrides
    cli_overrides = {}
    for item in getattr(args, "set", []) or []:
        if "=" in item:
            k, _, v = item.partition("=")
            cli_overrides[k.strip()] = v.strip()

    resolver = TemplateResolver()
    try:
        result = resolver.resolve_dry_run(path, variables=cli_overrides)
    except Exception as e:
        print(f"Error: {e}")
        return 1

    for filename, content in result["files"].items():
        print(f"=== {filename} ===")
        print(content)
        print()

    unresolved = result.get("unresolved", {})
    if unresolved:
        print("Unresolved variables:")
        for filename, var_names in unresolved.items():
            for v in var_names:
                print(f"  {filename}: {{ {v} }}")
    else:
        print("All variables resolved successfully.")
    return 0


def _cmd_agent_import(args: argparse.Namespace) -> int:
    """Reverse-engineer an existing agent into a template bundle."""
    from robothor.templates.installer import import_agent

    agent_id = args.agent_id
    output = getattr(args, "output", None)

    try:
        result = import_agent(agent_id, output_dir=output)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    print(f"Imported: {result['agent_id']}")
    print(f"  Output: {result['output_dir']}")
    print("  Files:")
    for f in result.get("files", []):
        print(f"    {f}")
    variables = result.get("variables", {})
    if variables:
        print(f"  Variables ({len(variables)}):")
        for k, v in variables.items():
            default = v.get("default", "") if isinstance(v, dict) else v
            print(f"    {k} = {default}")
    return 0


def _cmd_agent_setup() -> int:
    """Interactive onboarding wizard for new instances."""
    from robothor.templates.catalog import Catalog
    from robothor.templates.installer import install
    from robothor.templates.instance import InstanceConfig

    instance = InstanceConfig.load()

    # 1. Check/create instance config
    if not instance.exists:
        print("=== Genus OS Agent Setup ===\n")
        print("Setting up instance configuration...\n")

        tz = input("  Timezone [America/New_York]: ").strip() or "America/New_York"
        model = (
            input("  Default model [openrouter/xiaomi/mimo-v2-pro]: ").strip()
            or "openrouter/xiaomi/mimo-v2-pro"
        )
        quality = (
            input("  Quality model [openrouter/anthropic/claude-sonnet-4.6]: ").strip()
            or "openrouter/anthropic/claude-sonnet-4.6"
        )
        owner = input("  Owner name: ").strip()

        instance.init_config(
            timezone=tz,
            default_model=model,
            quality_model=quality,
            owner_name=owner,
        )
        print(f"\n  Config saved to {instance.config_path}\n")
    else:
        print("Instance config found.\n")

    # 2. Show presets
    catalog = Catalog()
    presets = catalog.list_presets()

    print("Available presets:")
    for i, preset in enumerate(presets, 1):
        count = len(preset["agents"])
        print(f"  {i}. {preset['id']:<12} -- {preset['description']} ({count} agents)")
    print(f"  {len(presets) + 1}. custom     -- Pick departments individually")
    print()

    choice = input(f"  Select preset [1-{len(presets) + 1}]: ").strip()

    agent_ids: list[str] = []
    if choice.isdigit() and 1 <= int(choice) <= len(presets):
        selected_preset = presets[int(choice) - 1]
        agent_ids = selected_preset["agents"]
        print(f"\n  Selected: {selected_preset['id']} ({len(agent_ids)} agents)")
    elif choice.isdigit() and int(choice) == len(presets) + 1:
        # Custom: pick departments
        departments = catalog.list_departments()
        print("\n  Departments:")
        for i, dept in enumerate(departments, 1):
            print(f"    {i}. {dept['name']:<25} ({len(dept['agents'])} agents)")
        selection = input("  Select departments (comma-separated numbers): ").strip()
        for part in selection.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(departments):
                    agent_ids.extend(departments[idx]["agents"])
    else:
        print("Invalid selection.")
        return 1

    if not agent_ids:
        print("No agents selected.")
        return 0

    # 3. Install selected agents
    print(f"\nInstalling {len(agent_ids)} agents...\n")
    installed = 0
    for agent_id in agent_ids:
        template_path = catalog.find_template(agent_id)
        if not template_path:
            print(f"  {agent_id}: no template found (use 'robothor agent import' first)")
            continue
        try:
            result = install(str(template_path), auto_yes=True)
            print(f"  {agent_id}: installed (v{result['version']})")
            installed += 1
        except Exception as e:
            print(f"  {agent_id}: FAILED -- {e}")

    # 4. Summary
    print(f"\n{installed}/{len(agent_ids)} agents installed.")
    if installed > 0:
        print("\nNext steps:")
        print("  python scripts/validate_agents.py")
        print("  sudo systemctl restart robothor-engine")
    return 0


def _cmd_agent_search(args: argparse.Namespace) -> int:
    """Search the hub for agent templates."""
    from robothor.templates.hub_client import HubClient, HubError

    query = args.query
    department = getattr(args, "department", None)

    try:
        with HubClient() as hub:
            results = hub.search(query, department=department)
    except HubError as e:
        print(f"Hub error: {e}")
        return 1
    except Exception as e:
        print(f"Error connecting to hub: {e}")
        return 1

    if not results:
        print("No agents found.")
        return 0

    print(f"\n{'Name':<30} {'Dept':<15} {'Version':<12} {'Downloads':<10}")
    print("-" * 67)
    for b in results:
        name = b.get("name", b.get("slug", "?"))[:29]
        dept = (b.get("department") or "-")[:14]
        ver = (b.get("version") or "-")[:11]
        dl = str(b.get("downloadCount", 0))
        premium = " $" if b.get("isPremium") else ""
        print(f"{name:<30} {dept:<15} {ver:<12} {dl:<10}{premium}")

    print(f"\n{len(results)} result(s). Install with: robothor agent install <slug>")
    return 0


def _cmd_agent_publish(args: argparse.Namespace) -> int:
    """Publish a template bundle to the hub."""
    from robothor.templates.hub_client import HubClient, HubError

    repo_url = args.repo_url

    try:
        with HubClient() as hub:
            bundle = hub.submit(repo_url)
    except HubError as e:
        print(f"Publish error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    print(f"Published: {bundle.get('name', '?')} ({bundle.get('slug', '?')})")
    print(f"View at: https://programmaticresources.com/bundle/{bundle.get('slug', '')}")
    return 0


def _cmd_agent_bind(args: argparse.Namespace) -> int:
    """Bind an agent to a channel/cron schedule by updating its manifest YAML."""
    manifest_dir = Path.home() / "robothor" / "docs" / "agents"
    manifest_path = manifest_dir / f"{args.agent_id}.yaml"

    if not manifest_path.exists():
        print(f"Error: No manifest found at {manifest_path}")
        return 1

    data, yaml_handler = _load_manifest(manifest_path)

    if args.cron:
        if "schedule" not in data:
            data["schedule"] = {}
        data["schedule"]["cron"] = args.cron

    if args.channel or args.to:
        if "delivery" not in data:
            data["delivery"] = {}
        if args.channel:
            data["delivery"]["channel"] = args.channel
            data["delivery"]["mode"] = "announce"
        if args.to:
            data["delivery"]["to"] = args.to

    _save_manifest(manifest_path, data, yaml_handler)

    changes = []
    if args.cron:
        changes.append(f"cron={args.cron}")
    if args.channel:
        changes.append(f"channel={args.channel}")
    if args.to:
        changes.append(f"to={args.to}")

    print(f"Updated {args.agent_id}: {', '.join(changes)}")
    print(f"Manifest: {manifest_path}")
    print("Restart the engine to apply: sudo systemctl restart robothor-engine")
    return 0


def _cmd_agent_unbind(args: argparse.Namespace) -> int:
    """Clear cron and set delivery to none for an agent."""
    manifest_dir = Path.home() / "robothor" / "docs" / "agents"
    manifest_path = manifest_dir / f"{args.agent_id}.yaml"

    if not manifest_path.exists():
        print(f"Error: No manifest found at {manifest_path}")
        return 1

    data, yaml_handler = _load_manifest(manifest_path)

    if "schedule" in data and "cron" in data["schedule"]:
        del data["schedule"]["cron"]
    if "delivery" in data:
        data["delivery"]["mode"] = "none"

    _save_manifest(manifest_path, data, yaml_handler)

    print(f"Unbound {args.agent_id}: cron cleared, delivery=none")
    print(f"Manifest: {manifest_path}")
    print("Restart the engine to apply: sudo systemctl restart robothor-engine")
    return 0
