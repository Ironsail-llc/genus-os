"""This engine's agent manifest files, over HTTP — for a bridge that cannot reach them.

One engine per tenant, each with its own workspace volume. The bridge's agent
builder writes the PLATFORM tenant's manifests directly (it shares a host with
the root engine); for a child tenant it reaches that tenant's engine and calls
these routes, which read and write through the same
:class:`~robothor.engine.manifest_store.ManifestStore` primitives.

These routes are a file store and nothing more. Merging a form's fields,
validating, bumping the version and auditing who did it all stay in the bridge,
which is where the operator's identity is; what lives here is only what has to
run next to the files. Under ``/api/admin``, so ``engine/auth.py`` requires the
``engine:control`` scope — and refuses a token minted for another tenant.

Every response carries ids, YAML the operator wrote and error TYPES. Never a
filesystem path (CLAUDE.md rules 1 and 2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class WriteBody(BaseModel):
    """A manifest's full text, and on a create its instruction file."""

    yaml: str
    create: bool = False
    instruction_file: str | None = None
    instructions: str | None = None


class ContextBody(BaseModel):
    """Workspace-relative paths a validator wants to know exist."""

    paths: list[str] = Field(default_factory=list)


def register(app: FastAPI) -> None:
    """Mount the manifest store routes on the engine app."""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    from robothor.engine.manifest_store import (
        NO_SUCH_AGENT,
        ManifestStore,
        default_model,
        failure_rows,
        main_agent_id,
    )
    from robothor.templates.safety import TemplateSecurityError

    router = APIRouter(prefix="/api/admin/manifests", tags=["admin"])

    def _refused(error: Exception) -> JSONResponse:
        # safety.py's message names the rule, never the path.
        return JSONResponse({"error": str(error)}, status_code=422)

    @router.get("")
    async def scan_manifests() -> Any:
        store = ManifestStore.from_env()

        def _scan() -> dict[str, Any]:
            scan = store.scan()
            return {
                "manifests": list(scan.manifests),
                "failures": failure_rows(scan),
                "main_agent_id": main_agent_id(),
                "default_model": default_model(store.manifest_dir),
            }

        try:
            return await asyncio.to_thread(_scan)
        except TemplateSecurityError as error:
            return _refused(error)

    @router.post("/context")
    async def validation_context(body: ContextBody) -> Any:
        """What a validator reads off disk: defaults, the schema, which files exist."""
        store = ManifestStore.from_env()

        def _context() -> dict[str, Any]:
            return {
                "defaults_yaml": store.defaults_text(),
                "schema_yaml": store.schema_text(),
                "existing": store.existing(list(body.paths)),
            }

        return await asyncio.to_thread(_context)

    @router.get("/{agent_id}")
    async def read_manifest(agent_id: str) -> Any:
        store = ManifestStore.from_env()

        def _read() -> dict[str, Any] | None:
            import yaml

            text = store.read_manifest(agent_id)
            if text is None:
                return None
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError:
                document = None
            relative = document.get("instruction_file") if isinstance(document, dict) else None
            return {"yaml": text, "instructions": store.read_instructions(relative)}

        try:
            found = await asyncio.to_thread(_read)
        except TemplateSecurityError as error:
            return _refused(error)
        if found is None:
            return JSONResponse({"error": NO_SUCH_AGENT}, status_code=404)
        return found

    @router.put("/{agent_id}")
    async def write_manifest(agent_id: str, body: WriteBody) -> Any:
        """Create (``create: true``, 409 if it exists) or replace (404 if absent)."""
        store = ManifestStore.from_env()

        def _write() -> None:
            if body.create:
                store.create(agent_id, body.yaml, body.instruction_file, body.instructions)
            else:
                store.update(agent_id, body.yaml)

        try:
            await asyncio.to_thread(_write)
        except TemplateSecurityError as error:
            return _refused(error)
        except FileExistsError:
            return JSONResponse({"error": "an agent with that id already exists"}, status_code=409)
        except FileNotFoundError:
            return JSONResponse({"error": NO_SUCH_AGENT}, status_code=404)
        except OSError as error:
            logger.warning("Manifest write failed: %s", type(error).__name__)
            return JSONResponse(
                {"error": "the agent manifest directory is not writable"}, status_code=422
            )
        return {"written": True}

    @router.post("/{agent_id}/retire")
    async def retire_manifest(agent_id: str) -> Any:
        store = ManifestStore.from_env()
        try:
            await asyncio.to_thread(store.retire, agent_id)
        except TemplateSecurityError as error:
            return _refused(error)
        except FileNotFoundError:
            return JSONResponse({"error": NO_SUCH_AGENT}, status_code=404)
        return {"retired": True}

    app.include_router(router)
