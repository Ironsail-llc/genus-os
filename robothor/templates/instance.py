"""
Instance configuration — manages the .robothor/ directory.

The .robothor/ directory tracks:
  - config.yaml:    Instance defaults (timezone, model, delivery target, hub URL)
  - installed.yaml: Installed agents (source, version, variables used)
  - overrides/:     Per-agent user customizations (preserved across updates)
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from robothor.templates.safety import contained_path, validate_identifier, validate_sha256


def _find_instance_dir() -> Path:
    """Find the .robothor/ directory (workspace root)."""
    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    return workspace / ".robothor"


class InstanceConfig:
    """Manages the .robothor/ directory and its files."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _find_instance_dir()
        self.config_path = self.base_dir / "config.yaml"
        self.installed_path = self.base_dir / "installed.yaml"
        self.overrides_dir = self.base_dir / "overrides"
        self.archive_dir = self.base_dir / "archive"

    def _path(self, relative: str, *, label: str) -> Path:
        """Resolve an instance-owned path without following an escaping symlink."""

        return contained_path(self.base_dir, relative, label=label)

    @classmethod
    def load(cls, base_dir: Path | None = None) -> InstanceConfig:
        """Load or create instance config."""
        instance = cls(base_dir)
        instance.base_dir.mkdir(parents=True, exist_ok=True)
        instance.base_dir = instance.base_dir.resolve(strict=True)
        instance.config_path = instance._path("config.yaml", label="instance config path")
        instance.installed_path = instance._path("installed.yaml", label="install registry path")
        instance.overrides_dir = instance._path("overrides", label="override directory")
        instance.archive_dir = instance._path("archive", label="archive directory")
        instance.overrides_dir.mkdir(exist_ok=True)
        return instance

    @property
    def exists(self) -> bool:
        """Check if instance config exists."""
        return self._path("config.yaml", label="instance config path").exists()

    @property
    def config(self) -> dict[str, Any]:
        """Load config.yaml."""
        config_path = self._path("config.yaml", label="instance config path")
        if config_path.exists():
            return yaml.safe_load(config_path.read_text()) or {}
        return {}

    @config.setter
    def config(self, data: dict[str, Any]) -> None:
        """Write config.yaml."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._path("config.yaml", label="instance config path").write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False)
        )

    @property
    def installed_agents(self) -> dict[str, Any]:
        """Load installed.yaml agents section."""
        installed_path = self._path("installed.yaml", label="install registry path")
        if installed_path.exists():
            data = yaml.safe_load(installed_path.read_text()) or {}
            agents = data.get("agents", {})
            if not isinstance(agents, dict):
                raise ValueError("Invalid installed agent registry: agents must be a mapping")
            return dict(agents)
        return {}

    def _save_installed(self, agents: dict[str, Any]) -> None:
        """Write installed.yaml."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._path("installed.yaml", label="install registry path").write_text(
            yaml.dump({"agents": agents}, default_flow_style=False, sort_keys=False)
        )

    def record_install(
        self,
        agent_id: str,
        source: str,
        source_path: str,
        version: str,
        variables: dict[str, Any],
        manifest_path: str,
        instruction_path: str,
        source_sha256: str | None = None,
    ) -> None:
        """Record an agent installation."""
        agent_id = validate_identifier(agent_id, label="agent ID")
        if source not in {"local", "hub"}:
            raise ValueError(f"Unsupported agent source: {source}")
        if source == "hub":
            validate_identifier(source_path, label="hub bundle slug")
            source_sha256 = validate_sha256(source_sha256, label="hub bundle SHA-256")

        agents = self.installed_agents
        agents[agent_id] = {
            "source": source,
            "source_path": source_path,
            "version": version,
            "installed_at": datetime.now(UTC).isoformat(),
            "variables": variables,
            "files": {
                "manifest": manifest_path,
                "instruction": instruction_path,
            },
        }
        if source_sha256 is not None:
            agents[agent_id]["source_sha256"] = source_sha256
        self._save_installed(agents)

    def record_remove(self, agent_id: str) -> dict[str, Any] | None:
        """Remove an agent from installed.yaml. Returns the removed record or None."""
        agent_id = validate_identifier(agent_id, label="agent ID")
        agents = self.installed_agents
        record: dict[str, Any] | None = agents.pop(agent_id, None)
        if record is not None:
            self._save_installed(agents)
        return record

    def get_agent_overrides(self, agent_id: str) -> dict[str, Any]:
        """Load per-agent overrides from overrides/<agent_id>.yaml."""
        agent_id = validate_identifier(agent_id, label="agent ID")
        override_path = self._path(f"overrides/{agent_id}.yaml", label="agent override path")
        if override_path.exists():
            return yaml.safe_load(override_path.read_text()) or {}
        return {}

    def save_agent_overrides(self, agent_id: str, overrides: dict[str, Any]) -> None:
        """Save per-agent overrides."""
        agent_id = validate_identifier(agent_id, label="agent ID")
        self.overrides_dir.mkdir(parents=True, exist_ok=True)
        override_path = self._path(f"overrides/{agent_id}.yaml", label="agent override path")
        override_path.write_text(yaml.dump(overrides, default_flow_style=False, sort_keys=False))

    def archive_agent(self, agent_id: str, files: dict[str, Path]) -> Path:
        """Archive agent files to .robothor/archive/<agent_id>/."""
        agent_id = validate_identifier(agent_id, label="agent ID")
        archive_path = self._path(f"archive/{agent_id}", label="agent archive path")
        archive_path.mkdir(parents=True, exist_ok=True)
        for src in files.values():
            if src.is_symlink():
                raise ValueError(f"Refusing to archive symlink: {src}")
            if src.is_file():
                raw_destination = archive_path / src.name
                if raw_destination.is_symlink():
                    raise ValueError(f"Refusing to overwrite archive symlink: {raw_destination}")
                dst = contained_path(archive_path, src.name, label="archived file path")
                shutil.copy2(src, dst)
        return archive_path

    def init_config(
        self,
        timezone: str = "America/New_York",
        default_model: str = "openrouter/xiaomi/mimo-v2-pro",
        quality_model: str = "openrouter/anthropic/claude-sonnet-4.6",
        owner_name: str = "",
        hub_org: str = "programmaticresources",
    ) -> dict[str, Any]:
        """Initialize a fresh config.yaml with defaults."""
        config = {
            "instance": {
                "timezone": timezone,
                "default_model": default_model,
                "quality_model": quality_model,
                "owner_name": owner_name,
                "hub_org": hub_org,
            },
            "defaults": {
                "delivery_mode": "none",
                "reports_to": "main",
                "escalates_to": "main",
                "bootstrap_files": [
                    "brain/AGENTS.md",
                    "brain/TOOLS.md",
                ],
            },
        }
        self.config = config
        return config
