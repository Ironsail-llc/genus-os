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
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    trusted_directory,
    validate_identifier,
)

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


def _catalog_bundle_for_agent(agent_id: str) -> Path | None:
    """Resolve an ID to a bundle discovered under the trusted local catalog.

    The URL value is validated as an identifier and used only for equality
    against directory entries.  It is never interpolated into a filesystem
    path.  This is intentionally stricter than consulting mutable install
    records, whose source paths are provenance rather than authorization.
    """

    from robothor.templates.catalog import Catalog

    safe_agent_id = validate_identifier(agent_id, label="agent ID")
    configured_root = Path(Catalog().catalog_dir)
    if not configured_root.exists():
        return None
    catalog_root = trusted_directory(configured_root, label="template catalog")

    for department in catalog_root.iterdir():
        if department.is_symlink() or not department.is_dir():
            continue
        for bundle in department.iterdir():
            if bundle.name != safe_agent_id or bundle.is_symlink() or not bundle.is_dir():
                continue
            candidate = trusted_directory(bundle, label="template bundle")
            return candidate
    return None


def _has_agent_manifest(agent_id: str) -> bool:
    """Check the canonical manifest root without trusting installed.yaml paths."""

    safe_agent_id = validate_identifier(agent_id, label="installed agent ID")
    if not MANIFEST_DIR.exists():
        return False
    manifest_root = trusted_directory(MANIFEST_DIR, label="agent manifest root")
    manifest = contained_path(
        manifest_root,
        f"{safe_agent_id}.yaml",
        label="installed agent manifest",
    )
    return manifest.is_file()


class InstallRequest(BaseModel):
    slug: str
    variables: dict[str, str] = {}


class UpdateRequest(BaseModel):
    pass  # No body needed — uses existing agent_id from path


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("")
def list_installed_agents() -> dict[str, object]:
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
        if not isinstance(meta, dict):
            logger.warning("Skipping malformed installed-agent record")
            continue
        try:
            safe_agent_id = validate_identifier(agent_id, label="installed agent ID")
            has_manifest = _has_agent_manifest(safe_agent_id)
        except TemplateSecurityError:
            logger.warning("Skipping installed-agent record with unsafe ID or manifest path")
            continue
        agent_info = {
            "agent_id": safe_agent_id,
            "version": meta.get("version", "unknown"),
            "installed_at": meta.get("installed_at", ""),
            "source": meta.get("source", ""),
            "department": meta.get("department", ""),
        }
        agent_info["has_manifest"] = has_manifest
        agents.append(agent_info)

    return {"agents": agents, "count": len(agents)}


@router.post("/install")
def install_agent(req: InstallRequest) -> dict[str, object]:
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
        logger.error("Install failed for %s: %s", sanitize_log(req.slug), sanitize_log(e))
        raise HTTPException(status_code=500, detail="internal error") from e


@router.post("/{agent_id}/update")
def update_agent(agent_id: str) -> dict[str, object]:
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
        logger.error("Update failed for %s: %s", sanitize_log(agent_id), sanitize_log(e))
        raise HTTPException(status_code=500, detail="internal error") from e


@router.delete("/{agent_id}")
def remove_agent(agent_id: str) -> dict[str, object]:
    """Remove an installed agent."""
    try:
        from robothor.templates.installer import remove

        remove(agent_id)
        return {"status": "removed", "agent_id": agent_id}
    except Exception as e:
        logger.error("Remove failed for %s: %s", sanitize_log(agent_id), sanitize_log(e))
        raise HTTPException(status_code=500, detail="internal error") from e


@router.get("/{agent_id}/readiness")
def check_readiness(agent_id: str) -> dict[str, object]:
    """Check hub readiness score for an agent."""
    try:
        from dataclasses import asdict

        from robothor.templates.description_optimizer import score_hub_readiness

        bundle = _catalog_bundle_for_agent(agent_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="Local agent template not found")
        report = score_hub_readiness(bundle)
        return {"agent_id": agent_id, "readiness": asdict(report)}
    except TemplateSecurityError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Readiness check failed for %s: %s", sanitize_log(agent_id), sanitize_log(e))
        raise HTTPException(status_code=500, detail="internal error") from e
