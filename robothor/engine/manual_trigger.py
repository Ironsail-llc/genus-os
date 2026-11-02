"""Fire one agent run now, outside its schedule.

The body of ``POST /api/agents/{id}/trigger``, lifted out of ``health.py`` so
the agent builder's ``run`` route (``admin_manifests.py``) fires an agent the
same way rather than over HTTP to its own process. Two callers, one function:
a second copy of "load the config, refuse a broken manifest, launch the run"
is how the button and the API come to disagree about which agents exist.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from robothor.auth.deps import AuthContext

logger = logging.getLogger(__name__)


async def trigger_agent(
    runner: Any,
    manifest_dir: Path,
    agent_id: str,
    auth: Callable[[], AuthContext] | None,
) -> dict[str, Any]:
    """Launch one MANUAL run of ``agent_id`` and answer without waiting for it.

    Answers ``{"error": ...}`` (not a raise) for a missing runner, a manifest
    the schema refuses and an agent that does not exist — the three are
    reported in different words, because broken and absent have different
    fixes. ``auth`` is called only once the run is actually launched, so a
    refusal never needs an identity.
    """
    if not runner:
        return {"error": "Runner not available"}
    from robothor.engine.config import load_agent_config
    from robothor.engine.manifest_schema import ManifestSchemaError
    from robothor.engine.models import TriggerType

    try:
        agent_config = load_agent_config(agent_id, manifest_dir)
    except ManifestSchemaError as e:
        # Broken, not absent, and the caller must be able to tell: the fix
        # for one is editing a manifest, for the other creating an agent.
        # An unguarded raise here was a 500 with a traceback.
        logger.error(
            "Trigger refused: %s manifest failed the schema: %s",
            sanitize_log(agent_id),
            sanitize_log(e.summary()),
        )
        return {"error": f"Agent manifest refused by the schema (SchemaError): {agent_id}"}
    if not agent_config:
        return {"error": f"Agent not found: {agent_id}"}

    identity = auth() if auth is not None else None

    async def _go() -> None:
        try:
            await runner.execute(
                agent_id=agent_id,
                message=f"Manual trigger via /api/agents/{agent_id}/trigger at {datetime.now(UTC).isoformat()}",
                trigger_type=TriggerType.MANUAL,
                trigger_detail="api-manual-trigger",
                agent_config=agent_config,
                tenant_id=getattr(identity, "tenant_id", None),
                user_id=getattr(identity, "user_id", None),
                user_role=getattr(identity, "role", None),
            )
        except Exception:
            logger.exception("Manual trigger of %s failed", sanitize_log(agent_id))

    asyncio.create_task(_go())
    return {"status": "started", "agent_id": agent_id}
