"""``plugins_overview`` -- what every installed plugin contributes, in one call.

The loader's report (:func:`robothor.plugins.inventory.inventory`) knows each
distribution's tools and doctor checks; the asset ledger and ``installed.yaml``
(:mod:`robothor.plugins.overview`) know the agents, workflows, manifest patches
and files it wrote into the workspace. An agent asked "what does this plugin
give us?" had to read all three by hand. Read-only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Manifest ``declared`` keys: the ``genus.tools`` payload key and the
#: ``genus.doctor`` one (``loader._GROUPS``).
_TOOLS_KEY = "handlers"
_CHECKS_KEY = "checks"

#: A tool description is a model-facing paragraph; the overview needs the gist.
_DESCRIPTION_CHARS = 300


def _description(name: str) -> str:
    """The registered schema's description for *name*, or ``""``."""
    try:
        from robothor.engine.tools.registry import get_registry

        schema = get_registry().get_schema(name)
    except Exception:  # noqa: BLE001 - a description is decoration, never a failure
        logger.debug("no schema for plugin tool %s", name, exc_info=True)
        return ""
    function = schema.get("function") if isinstance(schema, dict) else None
    text = function.get("description") if isinstance(function, dict) else None
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= _DESCRIPTION_CHARS else text[: _DESCRIPTION_CHARS - 1] + "…"


def _declared(manifest: dict[str, Any] | None, key: str) -> list[str]:
    declared = (manifest or {}).get("declared") or {}
    names = declared.get(key) if isinstance(declared, dict) else None
    return sorted(str(n) for n in names) if isinstance(names, list | tuple | set) else []


def plugins_overview() -> dict[str, Any]:
    """Every installed plugin and what it contributes. Blocking; call off the loop."""
    from robothor.plugins.assets import canonical
    from robothor.plugins.inventory import inventory
    from robothor.plugins.overview import empty_assets, workspace_assets

    warnings: list[str] = []
    assets = workspace_assets(warnings=warnings)
    plugins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in inventory():
        key = canonical(row.name)
        seen.add(key)
        written = assets.get(key) or {}
        plugins.append(
            {
                "name": row.name,
                "version": row.version or written.get("version") or "",
                "state": row.state,
                "tools": [
                    {"name": name, "description": _description(name)}
                    for name in _declared(row.manifest, _TOOLS_KEY)
                ],
                "checks": _declared(row.manifest, _CHECKS_KEY),
                **{k: written.get(k, v) for k, v in empty_assets().items()},
            }
        )
    # A plugin that ships only workspace files publishes no entry point, so the
    # loader never sees it; the ledger is the only record that it is there.
    for key, written in sorted(assets.items()):
        if key in seen:
            continue
        plugins.append(
            {
                "name": key,
                "version": written.get("version") or "",
                "state": "assets only",
                "tools": [],
                "checks": [],
                **{k: written.get(k, v) for k, v in empty_assets().items()},
            }
        )
    result: dict[str, Any] = {"plugins": plugins}
    if warnings:
        result["warnings"] = warnings
    return result


async def _plugins_overview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Tools, checks, agents, workflows, patches and files per installed plugin."""
    try:
        return await asyncio.to_thread(plugins_overview)
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        logger.warning("plugins_overview failed: %s", exc)
        return {"error": f"plugins_overview failed: {type(exc).__name__}: {exc}"}


HANDLERS["plugins_overview"] = _plugins_overview
