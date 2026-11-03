"""The view of the world a manifest verdict is judged against.

One fleet scan and one tool list per request, so an edit validates its BEFORE
and AFTER documents against the same world (``ValidationContext``). Lifted out
of ``admin_manifests`` so the builder's HTTP surface reads as its operations.
"""

from __future__ import annotations

import asyncio
import logging

from robothor.engine import manifest_store as store
from robothor.engine import manifest_validation as validation

logger = logging.getLogger("robothor.engine.admin_manifests")


async def registered_tools() -> tuple[set[str], list[dict[str, str]]]:
    """The registry's tool names, and a warning if it would not say.

    A registry that cannot be built downgrades the tool check to a warning
    rather than failing the save: the operator would otherwise be unable to
    edit an agent while a plugin is broken, which is exactly when they most
    want to.
    """
    from robothor.engine import admin_scheduler

    try:
        names = await asyncio.to_thread(admin_scheduler.registered_tool_names)
    except Exception as error:  # noqa: BLE001 — a warning, not a refusal
        logger.warning("Tool registry unavailable for validation: %s", type(error).__name__)
        return set(), [
            validation.issue(
                "tools_allowed",
                "tools_unverified",
                "the tool registry did not answer, so tool names were not checked",
            )
        ]
    return {str(name) for name in names}, []


async def validation_context() -> validation.ValidationContext:
    """One view of the world for every verdict in this request.

    Its own function so an edit can validate the BEFORE and AFTER documents
    against the same fleet and the same tool list — see ValidationContext.
    """
    scan = await asyncio.to_thread(store.scan)
    fleet = {str(m["id"]): m for m in scan.manifests if isinstance(m.get("id"), str)}
    tools, tool_warnings = await registered_tools()
    return validation.ValidationContext(
        fleet=fleet,
        tools=tools,
        manifest_dir=store.manifest_dir(),
        repo_root=store.workspace(),
        tool_warnings=tool_warnings,
    )
