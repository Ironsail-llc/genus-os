"""
Agent catalog — departments, presets, and template discovery.

Loads _catalog.yaml and _defaults.yaml from templates/agents/ and provides
browsing, filtering, and preset resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _find_catalog_dir() -> Path:
    """Find templates/agents/ directory."""
    # In-repo development
    repo_root = Path(__file__).resolve().parent.parent.parent
    dev_path = repo_root / "templates" / "agents"
    if dev_path.exists():
        return dev_path
    # Bundled in wheel
    bundled = Path(__file__).parent / "bundled_templates" / "agents"
    if bundled.exists():
        return bundled
    return dev_path  # Return expected path even if missing


class Catalog:
    """Browse and query the agent template catalog."""

    def __init__(self, catalog_dir: Path | None = None):
        self.catalog_dir = catalog_dir or _find_catalog_dir()
        self._catalog: dict[str, Any] | None = None
        self._defaults: dict[str, Any] | None = None

    @property
    def catalog(self) -> dict[str, Any]:
        if self._catalog is None:
            path = self.catalog_dir / "_catalog.yaml"
            if path.exists():
                self._catalog = yaml.safe_load(path.read_text()) or {}
            else:
                self._catalog = {"departments": {}, "presets": {}}
        return self._catalog

    @property
    def defaults(self) -> dict[str, Any]:
        if self._defaults is None:
            path = self.catalog_dir / "_defaults.yaml"
            if path.exists():
                self._defaults = yaml.safe_load(path.read_text()) or {}
            else:
                self._defaults = {}
        return self._defaults

    @property
    def departments(self) -> dict[str, Any]:
        return dict(self.catalog.get("departments", {}))

    @property
    def presets(self) -> dict[str, Any]:
        return dict(self.catalog.get("presets", {}))

    def list_departments(self) -> list[dict[str, Any]]:
        """List all departments with their agents."""
        result = []
        for dept_id, dept in self.departments.items():
            result.append(
                {
                    "id": dept_id,
                    "name": dept.get("name", dept_id),
                    "description": dept.get("description", ""),
                    "agents": dept.get("agents", []),
                }
            )
        return result

    def list_presets(self) -> list[dict[str, Any]]:
        """List all installation presets."""
        result = []
        for preset_id, preset in self.presets.items():
            agents = preset.get("agents", [])
            if agents == "all":
                # Collect all agents from all departments
                agents = []
                for dept in self.departments.values():
                    agents.extend(dept.get("agents", []))
            result.append(
                {
                    "id": preset_id,
                    "description": preset.get("description", ""),
                    "agents": agents,
                }
            )
        return result

    def get_preset_agents(self, preset_id: str) -> list[str]:
        """Get agent IDs for a preset."""
        preset = self.presets.get(preset_id)
        if not preset:
            return []
        agents = preset.get("agents", [])
        if agents == "all":
            agents = []
            for dept in self.departments.values():
                agents.extend(dept.get("agents", []))
        return list(agents)

    def get_department_agents(self, department_id: str) -> list[str]:
        """Get agent IDs for a department."""
        dept = self.departments.get(department_id)
        if not dept:
            return []
        return list(dept.get("agents", []))

    def find_template(self, agent_id: str) -> Path | None:
        """Find a template bundle by agent ID.

        Searches templates/agents/<dept>/<id>/ directories.
        """
        for dept_id, dept in self.departments.items():
            if agent_id in dept.get("agents", []):
                path = self.catalog_dir / dept_id / agent_id
                if path.is_dir():
                    return path

        # Fallback: search all subdirectories
        for dept_dir in self.catalog_dir.iterdir():
            if dept_dir.is_dir() and not dept_dir.name.startswith("_"):
                agent_dir = dept_dir / agent_id
                if agent_dir.is_dir() and (agent_dir / "setup.yaml").exists():
                    return agent_dir

        return None

    def list_available_templates(self) -> list[dict[str, Any]]:
        """List all available template bundles found on disk."""
        templates = []
        for dept_dir in sorted(self.catalog_dir.iterdir()):
            if not dept_dir.is_dir() or dept_dir.name.startswith("_"):
                continue
            for agent_dir in sorted(dept_dir.iterdir()):
                if not agent_dir.is_dir():
                    continue
                setup_path = agent_dir / "setup.yaml"
                if setup_path.exists():
                    setup = yaml.safe_load(setup_path.read_text()) or {}
                    templates.append(
                        {
                            "id": setup.get("agent_id", agent_dir.name),
                            "department": dept_dir.name,
                            "version": setup.get("version", "?"),
                            "path": str(agent_dir),
                        }
                    )
        return templates
