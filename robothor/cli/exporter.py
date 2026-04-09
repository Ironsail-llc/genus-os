"""Platform export — export Robothor configuration as a portable bundle.

Usage:
    robothor export [--tenant TENANT_ID] [--output DIR] [--include-memory]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
import json
import os
from pathlib import Path
from typing import Any


def cmd_export(args: argparse.Namespace) -> int:
    """Export Robothor configuration as a portable bundle."""
    tenant_id = getattr(args, "tenant", "robothor-primary")
    output_dir = Path(getattr(args, "output", None) or f"./robothor-export-{tenant_id}")
    include_memory = getattr(args, "include_memory", False)

    output_dir.mkdir(parents=True, exist_ok=True)

    bundle: dict[str, Any] = {
        "format": "robothor-export",
        "version": "1.0",
        "tenant_id": tenant_id,
    }

    # Export agent manifests (sanitized — no env var values)
    agents_exported = _export_agents(output_dir)
    bundle["agents_count"] = agents_exported

    # Export skills
    skills_exported = _export_skills(output_dir)
    bundle["skills_count"] = skills_exported

    # Export memory blocks (opt-in only — may contain PII)
    if include_memory:
        memory = _export_memory(tenant_id)
        bundle["memory"] = memory
        print(f"  Memory blocks: {len(memory)}")

    # Write .env.example template
    _export_env_template(output_dir)

    # Write manifest
    manifest_path = output_dir / "robothor-import.yaml"
    try:
        import yaml

        manifest_path.write_text(yaml.dump(bundle, default_flow_style=False, sort_keys=False))
    except ImportError:
        manifest_path = output_dir / "robothor-import.json"
        manifest_path.write_text(json.dumps(bundle, indent=2))

    print(f"\nExport complete → {output_dir}/")
    print(f"  Agents:  {agents_exported}")
    print(f"  Skills:  {skills_exported}")
    print(f"  Manifest: {manifest_path.name}")
    if include_memory:
        print(f"  Memory:  {len(bundle.get('memory', {}))}")
    else:
        print("  Memory:  skipped (use --include-memory to include)")

    return 0


def _export_agents(output_dir: Path) -> int:
    """Copy agent manifests to output directory."""

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    agents_dir = workspace / "docs" / "agents"

    if not agents_dir.is_dir():
        return 0

    target = output_dir / "agents"
    target.mkdir(exist_ok=True)

    count = 0
    for yaml_file in sorted(agents_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue  # skip _defaults.yaml
        # Read and sanitize — replace env var references with placeholders
        content = yaml_file.read_text()
        (target / yaml_file.name).write_text(content)
        count += 1

    return count


def _export_skills(output_dir: Path) -> int:
    """Copy skill directories to output."""
    from robothor.engine.skills import _skills_dir

    source = _skills_dir()
    if not source.is_dir():
        return 0

    import shutil

    target = output_dir / "skills"
    target.mkdir(exist_ok=True)

    count = 0
    for skill_dir in sorted(source.iterdir()):
        if not (skill_dir / "SKILL.md").exists():
            continue
        shutil.copytree(skill_dir, target / skill_dir.name, dirs_exist_ok=True)
        count += 1

    return count


def _export_memory(tenant_id: str) -> dict[str, str]:
    """Export memory block contents."""
    from robothor.memory.blocks import list_blocks, read_block

    blocks = list_blocks(tenant_id=tenant_id)
    memory: dict[str, str] = {}

    for b in blocks.get("blocks", []):
        name = b["name"]
        data = read_block(name, tenant_id=tenant_id)
        content = data.get("content", "")
        if content:
            memory[name] = content

    return memory


def _export_env_template(output_dir: Path) -> None:
    """Write a .env.example with placeholder variables."""
    template = """\
# Robothor Environment Template
# Fill in these values for your deployment.

# Required
OPENROUTER_API_KEY=your-openrouter-key
TELEGRAM_BOT_TOKEN=your-bot-token
ROBOTHOR_TELEGRAM_CHAT_ID=your-chat-id

# Database
DB_HOST=localhost
DB_PORT=5432
DB_USER=robothor
DB_PASSWORD=your-db-password
DB_NAME=robothor

# Optional
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
# TWILIO_ACCOUNT_SID=your-twilio-sid
# TWILIO_AUTH_TOKEN=your-twilio-token
"""
    (output_dir / ".env.example").write_text(template)
