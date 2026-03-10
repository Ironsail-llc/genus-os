"""
Genus OS Agent Template System — parameterized agent bundles with variable resolution.

This package provides:
  - resolver:         {{ variable }} resolution engine for template files
  - manifest_checks:  12 validation checks (A-L) extracted from validate_agents.py
  - validators:       Template-specific validation (SKILL.md, setup.yaml, resolution)
  - catalog:          Department/preset catalog from _catalog.yaml
  - installer:        Install/remove/update orchestration
  - instance:         .robothor/ directory management
  - hub_client:       GitHub API client (stubbed for future hub)
  - adapters:         Format adapters (robothor_native for now)

The engine (robothor/engine/) remains unchanged — it reads docs/agents/*.yaml.
This is an authoring layer that outputs the same manifests the engine expects.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "resolve_template",
    "install_agent",
    "remove_agent",
    "list_installed",
]


def resolve_template(
    template_path: str, variables: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Convenience wrapper — resolve a template bundle and return file contents."""
    from robothor.templates.resolver import TemplateResolver

    resolver = TemplateResolver()
    return resolver.resolve_bundle(template_path, variables or {}, **kwargs)


def install_agent(template_path: str, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper — install an agent from a template bundle."""
    from robothor.templates.installer import install

    return install(template_path, **kwargs)


def remove_agent(agent_id: str, **kwargs: Any) -> bool:
    """Convenience wrapper — remove an installed agent."""
    from robothor.templates.installer import remove

    return remove(agent_id, **kwargs)


def list_installed() -> dict[str, Any]:
    """Convenience wrapper — list installed agents."""
    from robothor.templates.instance import InstanceConfig

    return InstanceConfig.load().installed_agents
