"""Resolve delegated agents inside the parent's reviewed fleet when pinned."""

from __future__ import annotations

import asyncio


async def load_child_config(agent_id, engine_config, release_id):
    if release_id is None:
        from robothor.engine.config import load_agent_config_or_reason

        return load_agent_config_or_reason(agent_id, engine_config.manifest_dir, "spawn")

    from robothor.templates.fleet_release import ReleaseError
    from robothor.templates.fleet_snapshot import load_snapshot
    from robothor.templates.fleet_store import staged_release_path

    try:
        root = staged_release_path(engine_config.workspace, release_id)
        snapshot = await asyncio.to_thread(load_snapshot, root, expected_digest=release_id)
        return snapshot.agent(agent_id), ""
    except (ReleaseError, ValueError, OSError):
        # An invalid selected release must never fall through to mutable live
        # manifests, including a same-named agent from another fleet.
        return (
            None,
            "Child agent's reviewed fleet release is unavailable, changed, or missing the agent",
        )
