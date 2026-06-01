"""Sanctioned CRM → engine/memory inversion seam.

The CRM data layer (``robothor.crm.dal``) must stay importable without dragging
in the engine or memory subsystems — that keeps the layering acyclic and lets
the bridge import the DAL standalone. A few DAL persistence paths genuinely need
engine/memory behavior (parsing a manifest into goal specs; seeding a new
tenant's memory blocks). Instead of importing the engine inline at those call
sites, the DAL calls through these hooks.

Each hook lazily binds to the real implementation on first call — behavior is
identical to the prior in-function imports, and importing ``robothor.crm.dal``
does NOT import the engine (the bind happens at call time, only in contexts that
actually reach these paths, all of which are engine/CLI/script-side today).
``register_*`` lets the engine pre-bind at startup or tests inject fakes.

This module is the ONE place ``robothor.crm`` is allowed to reference the engine;
``tests/test_import_boundaries.py`` enforces that nothing else does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

_goal_parser: Callable[[dict[str, Any]], list[Any]] | None = None
_tenant_seeder: Callable[[str], Any] | None = None


def register_goal_parser(fn: Callable[[dict[str, Any]], list[Any]]) -> None:
    """Override the manifest→goals parser (engine startup / tests)."""
    global _goal_parser
    _goal_parser = fn


def register_tenant_seeder(fn: Callable[[str], Any]) -> None:
    """Override the per-tenant memory-block seeder (engine startup / tests)."""
    global _tenant_seeder
    _tenant_seeder = fn


def parse_goals_from_manifest(manifest: dict[str, Any]) -> list[Any]:
    """Parse a manifest dict into goal specs. Defaults to the engine impl."""
    if _goal_parser is not None:
        return _goal_parser(manifest)
    from robothor.engine.goals import parse_goals_from_manifest as _impl

    return _impl(manifest)


def seed_blocks_for_tenant(tenant_id: str) -> Any:
    """Seed default memory blocks for a tenant. Defaults to the memory impl."""
    if _tenant_seeder is not None:
        return _tenant_seeder(tenant_id)
    from robothor.memory.blocks import seed_blocks_for_tenant as _impl

    return _impl(tenant_id)
