"""
Instance configuration — manages the .robothor/ directory.

The .robothor/ directory tracks:
  - config.yaml:    Instance defaults (timezone, model, delivery target, hub URL)
  - installed.yaml: Installed agents (source, version, variables used)
  - overrides/:     Per-agent user customizations (preserved across updates)
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def _find_instance_dir() -> Path:
    """Find the .robothor/ directory (workspace root)."""
    workspace = Path.home() / "robothor"
    return workspace / ".robothor"


class InstanceConfig:
    """Manages the .robothor/ directory and its files."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _find_instance_dir()
        self.config_path = self.base_dir / "config.yaml"
        self.installed_path = self.base_dir / "installed.yaml"
        self.overrides_dir = self.base_dir / "overrides"
        self.archive_dir = self.base_dir / "archive"

    @classmethod
    def load(cls, base_dir: Path | None = None) -> InstanceConfig:
        """Load or create instance config."""
        instance = cls(base_dir)
        instance.base_dir.mkdir(parents=True, exist_ok=True)
        instance.overrides_dir.mkdir(exist_ok=True)
        return instance

    @property
    def exists(self) -> bool:
        """Check if instance config exists."""
        return self.config_path.exists()

    @property
    def config(self) -> dict[str, Any]:
        """Load config.yaml."""
        if self.config_path.exists():
            return yaml.safe_load(self.config_path.read_text()) or {}
        return {}

    @config.setter
    def config(self, data: dict[str, Any]) -> None:
        """Write config.yaml."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    @property
    def installed_agents(self) -> dict[str, Any]:
        """Load installed.yaml agents section."""
        if self.installed_path.exists():
            data = yaml.safe_load(self.installed_path.read_text()) or {}
            return dict(data.get("agents", {}))
        return {}

    def _save_installed(self, agents: dict[str, Any]) -> None:
        """Write installed.yaml."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.installed_path.write_text(
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
    ) -> None:
        """Record an agent installation."""
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
        self._save_installed(agents)

    def record_remove(self, agent_id: str) -> dict[str, Any] | None:
        """Remove an agent from installed.yaml. Returns the removed record or None."""
        agents = self.installed_agents
        record: dict[str, Any] | None = agents.pop(agent_id, None)
        if record is not None:
            self._save_installed(agents)
        return record

    def get_agent_overrides(self, agent_id: str) -> dict[str, Any]:
        """Load per-agent overrides from overrides/<agent_id>.yaml."""
        override_path = self.overrides_dir / f"{agent_id}.yaml"
        if override_path.exists():
            return yaml.safe_load(override_path.read_text()) or {}
        return {}

    def save_agent_overrides(self, agent_id: str, overrides: dict[str, Any]) -> None:
        """Save per-agent overrides."""
        self.overrides_dir.mkdir(parents=True, exist_ok=True)
        override_path = self.overrides_dir / f"{agent_id}.yaml"
        override_path.write_text(yaml.dump(overrides, default_flow_style=False, sort_keys=False))

    def archive_agent(self, agent_id: str, files: dict[str, Path]) -> Path:
        """Archive agent files to .robothor/archive/<agent_id>/."""
        archive_path = self.archive_dir / agent_id
        archive_path.mkdir(parents=True, exist_ok=True)
        for src in files.values():
            if src.exists():
                dst = archive_path / src.name
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
