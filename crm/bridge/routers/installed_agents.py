"""Installed agents management — install, update, remove agents from the hub.

Wraps the existing hub_client and installer modules to provide REST endpoints
for the Helm UI's marketplace panel.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """Installed-agent state is appliance-global, so deny secondary tenants."""
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    prefix="/api/installed-agents",
    tags=["installed-agents"],
    dependencies=[Depends(_require_primary_tenant)],
)

_WORKSPACE = os.environ.get("ROBOTHOR_WORKSPACE", str(Path("~/robothor").expanduser()))
MANIFEST_DIR = Path(
    os.getenv("AGENT_MANIFEST_DIR")
    or os.getenv("ROBOTHOR_MANIFEST_DIR")
    or str(Path(_WORKSPACE) / "docs" / "agents")
)


class InstallRequest(BaseModel):
    slug: str
    variables: dict[str, str] = {}


class UpdateRequest(BaseModel):
    pass  # No body needed — uses existing agent_id from path


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("")
def list_installed_agents():
    """List all installed agents with version and status info."""
    try:
        from robothor.templates.instance import InstanceConfig

        config = InstanceConfig.load()
        installed = config.installed_agents or {}
    except Exception:
        installed = {}

    # Enrich with manifest data
    agents = []
    for agent_id, meta in installed.items():
        agent_info = {
            "agent_id": agent_id,
            "version": meta.get("version", "unknown"),
            "installed_at": meta.get("installed_at", ""),
            "source": meta.get("source", ""),
            "department": meta.get("department", ""),
        }
        # Check if manifest exists
        manifest_path = MANIFEST_DIR / f"{agent_id}.yaml"
        agent_info["has_manifest"] = manifest_path.exists()
        agents.append(agent_info)

    return {"agents": agents, "count": len(agents)}


@router.post("/install")
def install_agent(req: InstallRequest):
    """Install an agent from the Programmatic Resources hub."""
    try:
        from robothor.templates.hub_client import HubClient, trusted_bundle_sha256
        from robothor.templates.installer import install

        with HubClient() as client:
            metadata = client.get_bundle(req.slug)
            if metadata is None:
                raise HTTPException(status_code=404, detail=f"Bundle not found: {req.slug}")
            expected_sha256 = trusted_bundle_sha256(metadata)
            bundle = client.download_bundle(req.slug, expected_sha256=expected_sha256)

        result = install(
            bundle,
            overrides=req.variables,
            auto_yes=True,
            source="hub",
            source_ref=req.slug,
            source_sha256=expected_sha256,
        )
        return {
            "status": "installed",
            "agent_id": result.get("agent_id", req.slug),
            "files_created": result.get("files_created", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Install failed for %s: %s", sanitize_log(req.slug), e)
        raise HTTPException(status_code=500, detail="internal error") from e


@router.post("/{agent_id}/update")
def update_agent(agent_id: str):
    """Update an installed agent to the latest version."""
    try:
        from robothor.templates.hub_client import HubClient, trusted_bundle_sha256
        from robothor.templates.installer import update

        with HubClient() as client:
            metadata = client.get_bundle(agent_id)
            if metadata is None:
                raise HTTPException(status_code=404, detail=f"Bundle not found: {agent_id}")
            expected_sha256 = trusted_bundle_sha256(metadata)
            bundle = client.download_bundle(agent_id, expected_sha256=expected_sha256)

        result = update(
            agent_id,
            new_template_path=bundle,
            auto_yes=True,
            source="hub",
            source_ref=agent_id,
            source_sha256=expected_sha256,
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"Agent not installed: {agent_id}")
        return {
            "status": "updated",
            "agent_id": agent_id,
            "new_version": result.get("version", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Update failed for %s: %s", sanitize_log(agent_id), e)
        raise HTTPException(status_code=500, detail="internal error") from e


@router.delete("/{agent_id}")
def remove_agent(agent_id: str):
    """Remove an installed agent."""
    try:
        from robothor.templates.installer import remove

        remove(agent_id)
        return {"status": "removed", "agent_id": agent_id}
    except Exception as e:
        logger.error("Remove failed for %s: %s", sanitize_log(agent_id), e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/readiness")
def check_readiness(agent_id: str):
    """Check hub readiness score for an agent."""
    try:
        from robothor.templates.description_optimizer import score_hub_readiness

        score = score_hub_readiness(agent_id)
        return {"agent_id": agent_id, "readiness": score}
    except Exception as e:
        logger.error("Readiness check failed for %s: %s", sanitize_log(agent_id), e)
        raise HTTPException(status_code=500, detail=str(e)) from e
