"""Installed agents management — install, update, remove agents from the hub.

Wraps the existing hub_client and installer modules to provide REST endpoints
for the Helm UI's marketplace panel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from robothor.engine.sanitize import sanitize_log
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    trusted_directory,
    validate_identifier,
)
from routers._audit import audited
from routers._operator import require_operator
from routers.agent_manifests import reconcile_engine_schedules

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
async def install_agent(req: InstallRequest, request: Request) -> dict[str, object]:
    """Install an agent from the Programmatic Resources hub.

    Operator-only and audited: this writes an agent manifest and runs the
    template installer against the appliance, which is not a member act.
    ``req.variables`` routinely carries credentials, so only the slug is
    recorded — never the variables.

    ``async`` with the install itself in a thread, so the route can await the
    engine reconcile afterwards. Without that call the manifest this just wrote
    sits inert until somebody restarts the engine, which is the whole reason the
    marketplace "installed" an agent that never ran.
    """
    require_operator(request)
    result = await asyncio.to_thread(_install_from_hub, req, request)
    result["reconcile"] = await reconcile_engine_schedules()
    return result


def _install_from_hub(req: InstallRequest, request: Request) -> dict[str, object]:
    """The blocking half: a hub download and the template installer."""
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
        agent_id = str(result.get("agent_id", req.slug))
        audited(request, "helm.agent.install", action=req.slug, agent_id=agent_id)
        return {
            "status": "installed",
            "agent_id": agent_id,
            "files_created": result.get("files_created", []),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Install failed for %s: %s", sanitize_log(req.slug), sanitize_log(e))
        audited(request, "helm.agent.install", action=req.slug, status="error")
        raise HTTPException(status_code=500, detail="internal error") from e


@router.post("/{agent_id}/update")
async def update_agent(agent_id: str, request: Request) -> dict[str, object]:
    """Update an installed agent to the latest version. Operator-only, audited.

    Reconciles afterwards: an update can change the agent's cron, and the
    engine is holding the trigger it parsed at boot until it is told otherwise.
    """
    require_operator(request)
    result = await asyncio.to_thread(_update_from_hub, agent_id, request)
    result["reconcile"] = await reconcile_engine_schedules()
    return result


def _update_from_hub(agent_id: str, request: Request) -> dict[str, object]:
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
        new_version = str(result.get("version", ""))
        audited(request, "helm.agent.update", action=agent_id, new_version=new_version)
        return {
            "status": "updated",
            "agent_id": agent_id,
            "new_version": new_version,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Update failed for %s: %s", sanitize_log(agent_id), sanitize_log(e))
        audited(request, "helm.agent.update", action=agent_id, status="error")
        raise HTTPException(status_code=500, detail="internal error") from e


@router.delete("/{agent_id}")
async def remove_agent(agent_id: str, request: Request) -> dict[str, object]:
    """Remove an installed agent. Operator-only, audited.

    Reconciles afterwards so the schedule goes with the manifest, instead of the
    engine firing a job for an agent whose config no longer exists.
    """
    require_operator(request)
    result = await asyncio.to_thread(_remove_installed, agent_id, request)
    result["reconcile"] = await reconcile_engine_schedules()
    return result


def _remove_installed(agent_id: str, request: Request) -> dict[str, object]:
    try:
        from robothor.templates.installer import remove

        remove(agent_id)
        audited(request, "helm.agent.remove", action=agent_id)
        return {"status": "removed", "agent_id": agent_id}
    except Exception as e:
        logger.error("Remove failed for %s: %s", sanitize_log(agent_id), sanitize_log(e))
        audited(request, "helm.agent.remove", action=agent_id, status="error")
        raise HTTPException(status_code=500, detail="internal error") from e


def _safe_agent_id(agent_id: str) -> str:
    try:
        return validate_identifier(agent_id, label="installed agent ID")
    except TemplateSecurityError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _build_export(agent_id: str, destination: Path) -> object:
    """Export *agent_id* into *destination*, translating refusals into HTTP.

    ``include_adapters`` is hard-coded off and is not a parameter. An adapter
    declares a COMMAND the engine will run and the credentials to run it with;
    deciding to hand that to somebody is not a decision a download button
    should be able to make on an operator's behalf. The CLI carries them,
    where the operator has typed the flag.
    """
    from robothor.templates.exporter import ExportError, export_agent

    try:
        return export_agent(agent_id, out=destination, include_adapters=False)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Agent not installed: {agent_id}") from error
    except ExportError as error:
        # 409, not 500: nothing is broken — the agent's own files carry
        # something that must not leave. str(error) names file:line for every
        # finding and never the text that triggered it, which is why it is safe
        # to put in a response body at all.
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{agent_id}/export")
async def export_installed_agent(agent_id: str, request: Request) -> Response:
    """Download an installed agent as a bundle. Operator-only, audited.

    Audited with the agent id and nothing else: the interesting content of an
    export is the agent's instructions, and an audit store read by auditors and
    shipped to a SIEM is the last place any of it should appear.
    """
    require_operator(request)
    return await asyncio.to_thread(_export_archive, agent_id, request)


def _export_archive(agent_id: str, request: Request) -> Response:
    safe_agent_id = _safe_agent_id(agent_id)
    from robothor.templates.exporter import archive_name

    try:
        with tempfile.TemporaryDirectory(prefix="genus-helm-export-") as scratch:
            # A fixed temp name, renamed only in the Content-Disposition: the
            # version is not known until the export has read the manifest, and
            # a filename assembled from an unvalidated id is a path.
            destination = Path(scratch) / "bundle.tar.gz"
            result = _build_export(safe_agent_id, destination)
            payload = destination.read_bytes()
    except HTTPException:
        audited(request, "helm.agent.export", action=safe_agent_id, status="denied")
        raise
    except Exception as error:
        logger.error("Export failed for %s: %s", sanitize_log(safe_agent_id), sanitize_log(error))
        audited(request, "helm.agent.export", action=safe_agent_id, status="error")
        raise HTTPException(status_code=500, detail="internal error") from error

    audited(request, "helm.agent.export", action=safe_agent_id)
    filename = archive_name(safe_agent_id, str(getattr(result, "version", "0.0.0")))
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{agent_id}/export/plan")
async def export_installed_agent_plan(agent_id: str, request: Request) -> dict[str, object]:
    """What an export WOULD contain, so the Helm can show it before downloading.

    Built by running the real export into a temporary directory and throwing the
    bytes away, rather than by predicting it. A plan derived from a second,
    cheaper code path is a plan that can disagree with the thing it describes —
    and the one field that must never disagree is the secret gate's verdict.
    """
    require_operator(request)
    return await asyncio.to_thread(_export_plan, agent_id, request)


def _export_plan(agent_id: str, request: Request) -> dict[str, object]:
    safe_agent_id = _safe_agent_id(agent_id)
    from robothor.templates.bundle import BUNDLE_KIND, BUNDLE_SCHEMA
    from robothor.templates.exporter import archive_name

    try:
        with tempfile.TemporaryDirectory(prefix="genus-helm-plan-") as scratch:
            result = _build_export(safe_agent_id, Path(scratch) / "bundle")
    except HTTPException:
        raise
    except Exception as error:
        logger.error(
            "Export plan failed for %s: %s", sanitize_log(safe_agent_id), sanitize_log(error)
        )
        raise HTTPException(status_code=500, detail="internal error") from error

    manifest = result.manifest  # type: ignore[attr-defined]
    return {
        "agent_id": manifest.id,
        "kind": BUNDLE_KIND,
        "schema": BUNDLE_SCHEMA,
        "name": manifest.name,
        "version": manifest.version,
        "platform_version": manifest.platform_version,
        "filename": archive_name(manifest.id, manifest.version),
        "requires": manifest.requires.as_document(),
        "files": [{"path": f.path, "sha256": f.sha256} for f in manifest.files],
        # Stated rather than implied. A Share dialog that does not say this
        # leaves an operator assuming their MCP connections travelled with the
        # agent, and the far side wondering why nothing works.
        "include_adapters": False,
        "note": (
            "Adapter definitions are never included over HTTP — an adapter names a "
            "command to run. Use 'genus agent export --include-adapters' if you mean "
            "to carry them. Installing from a path or a URL is CLI-only."
        ),
    }


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
