"""Where the ACTING tenant's agent manifests are, and how to reach them.

One engine per tenant, each with its own manifest directory. The platform
tenant's lives on this host, next to the bridge, and is read and written
directly. A child tenant's lives on that tenant's engine's volume, which the
bridge cannot reach — so for a child tenant every read and write goes through
that engine's ``/api/admin/manifests*`` routes (``robothor/engine/
admin_manifests.py``). Both sides use ``robothor.engine.manifest_store`` for the
file work, so an atomic write, the history ring and a retire mean one thing.

The routers above this module (``agent_manifests``, ``fleet``,
``automations``) keep everything that is not file work — merging a form's
fields, validation, the version bump, the audit event — and pick a backend with
:func:`backend_for`. A child tenant's admin is never shown the root fleet, and a
child's agent is never written into it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import HTTPException

from robothor.engine.manifest_store import (
    NO_SUCH_AGENT,
    ManifestStore,
    default_model,
    write_atomically,
)
from robothor.templates.safety import TemplateSecurityError
from routers import _operator
from routers._engine_client import engine_base_url, engine_request

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class Scan:
    """What a manifest directory holds: the readable documents and the failures.

    ``failures`` carry ``agent_id``, ``filename`` and ``error_type`` — the shape
    ``_manifest_rows._broken`` reads — and never a parser's detail.
    """

    manifests: list[dict[str, Any]]
    failures: list[Any] = field(default_factory=list)
    #: The tenant engine's main agent. ``None`` on a local scan, where the
    #: caller resolves it from this host's own engine configuration.
    main_agent_id: str | None = None


def _refused(error: TemplateSecurityError) -> HTTPException:
    """A containment refusal, as a 422 the operator can read.

    A symlinked ``brain/`` (or ``docs/agents/``) is a deployment the appliance
    will not write through. The message comes from ``safety.py`` and names the
    rule, never the path.
    """
    return HTTPException(status_code=422, detail=str(error))


def _relative_paths(document: dict[str, Any]) -> list[str]:
    """Every workspace file a validator checks the existence of, for one document."""
    paths: list[Any] = [document.get("instruction_file")]
    paths.extend(document.get("bootstrap_files") or [])
    warmup = document.get("warmup")
    if isinstance(warmup, dict):
        paths.extend(warmup.get("context_files") or [])
    return [p for p in paths if isinstance(p, str) and p and p != "null"]


class LocalBackend:
    """The platform tenant: this host's own manifest directory."""

    tenant: str | None = None

    def store(self) -> ManifestStore:
        return ManifestStore.from_env()

    def _guarded(self, call: Any, *args: Any) -> Any:
        try:
            return call(*args)
        except TemplateSecurityError as error:
            raise _refused(error) from error

    async def scan(self) -> Any:
        return await asyncio.to_thread(self.store().scan)

    def scan_sync(self) -> Any:
        return self.store().scan()

    async def default_model(self) -> str:
        return default_model(self.store().manifest_dir)

    async def read(self, agent_id: str) -> str | None:
        return await asyncio.to_thread(self._guarded, self.store().read_manifest, agent_id)

    async def read_instructions(self, document: dict[str, Any]) -> str:
        relative = document.get("instruction_file")
        return await asyncio.to_thread(self.store().read_instructions, relative)

    async def create(
        self, agent_id: str, text: str, instruction_relative: str, instructions: str
    ) -> None:
        try:
            await asyncio.to_thread(
                self._guarded,
                self.store().create,
                agent_id,
                text,
                instruction_relative,
                instructions,
            )
        except FileExistsError as error:
            # Two concurrent creates of the same id both pass the existence
            # check; O_EXCL is the decision.
            raise HTTPException(
                status_code=409, detail="an agent with that id already exists"
            ) from error
        except OSError as error:
            raise _unwritable(error) from error

    async def update(self, agent_id: str, text: str) -> None:
        try:
            await asyncio.to_thread(self._guarded, self.store().update, agent_id, text)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="no such agent") from error
        except OSError as error:
            raise _unwritable(error) from error

    async def retire(self, agent_id: str) -> None:
        try:
            await asyncio.to_thread(self._guarded, self.store().retire, agent_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="no such agent") from error

    @contextlib.asynccontextmanager
    async def validation_paths(
        self, documents: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[Path, Path]]:
        store = self.store()
        yield store.manifest_dir, store.workspace


def _unwritable(error: OSError) -> HTTPException:
    # The TYPE only: an OSError's str() carries the absolute path.
    logger.warning("Manifest directory unusable: %s", type(error).__name__)
    return HTTPException(status_code=422, detail="the agent manifest directory is not writable")


class RemoteBackend:
    """A child tenant: its own engine's manifest store, over HTTP."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    async def _call(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        status, body = await engine_request(method, path, json=json, tenant=self.tenant)
        if status == 404:
            if isinstance(body, dict) and body.get("error") == NO_SUCH_AGENT:
                raise HTTPException(status_code=404, detail=NO_SUCH_AGENT)
            # Any other 404 is the ROUTE missing: an engine that predates
            # /api/admin/manifests. Reporting it as "no such agent" would tell
            # the operator their agent is gone when their engine is old.
            logger.warning(
                "The engine at %s does not support manifest administration "
                "(no /api/admin/manifests); upgrade it",
                engine_base_url(self.tenant),
            )
            raise HTTPException(
                status_code=502,
                detail="the tenant's engine does not support manifest administration; "
                "upgrade the engine",
            )
        if status == 409:
            raise HTTPException(status_code=409, detail="an agent with that id already exists")
        if status == 422:
            detail = body.get("error") if isinstance(body, dict) else None
            raise HTTPException(status_code=422, detail=detail or "the engine refused the path")
        if status >= 400 or not isinstance(body, dict):
            raise HTTPException(
                status_code=502, detail="the tenant's engine did not answer the manifest call"
            )
        return body

    @staticmethod
    def _path(agent_id: str, suffix: str = "") -> str:
        # The id is already validated to [a-z0-9-]; encoded as well, because
        # it is the only part of an engine URL a caller chooses.
        return f"/api/admin/manifests/{quote(agent_id, safe='')}{suffix}"

    async def _scan_body(self) -> dict[str, Any]:
        body: dict[str, Any] = await self._call("GET", "/api/admin/manifests")
        return body

    async def scan(self) -> Scan:
        body = await self._scan_body()
        return Scan(
            manifests=[m for m in body.get("manifests") or [] if isinstance(m, dict)],
            failures=[
                SimpleNamespace(
                    agent_id=str(f.get("agent_id") or ""),
                    filename=str(f.get("filename") or ""),
                    error_type=str(f.get("error_type") or ""),
                )
                for f in body.get("failures") or []
                if isinstance(f, dict)
            ],
            main_agent_id=str(body.get("main_agent_id") or ""),
        )

    async def default_model(self) -> str:
        return str((await self._scan_body()).get("default_model") or "")

    async def read(self, agent_id: str) -> str | None:
        try:
            body = await self._call("GET", self._path(agent_id))
        except HTTPException as error:
            if error.status_code == 404:
                return None
            raise
        return str(body.get("yaml") or "")

    async def read_instructions(self, document: dict[str, Any]) -> str:
        agent_id = document.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            return ""
        try:
            body = await self._call("GET", self._path(agent_id))
        except HTTPException:
            return ""
        return str(body.get("instructions") or "")

    async def create(
        self, agent_id: str, text: str, instruction_relative: str, instructions: str
    ) -> None:
        await self._call(
            "PUT",
            self._path(agent_id),
            json={
                "yaml": text,
                "create": True,
                "instruction_file": instruction_relative,
                "instructions": instructions,
            },
        )

    async def update(self, agent_id: str, text: str) -> None:
        await self._call("PUT", self._path(agent_id), json={"yaml": text, "create": False})

    async def retire(self, agent_id: str) -> None:
        await self._call("POST", self._path(agent_id, "/retire"))

    @contextlib.asynccontextmanager
    async def validation_paths(
        self, documents: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[Path, Path]]:
        """A throwaway copy of exactly what the validators read off the tenant's disk.

        ``_defaults.yaml``, the canonical schema, and an empty placeholder for
        each referenced file the tenant's engine says exists. The validators
        then see the CHILD's workspace, not this host's.
        """
        wanted = sorted({p for document in documents for p in _relative_paths(document)})
        body = await self._call("POST", "/api/admin/manifests/context", json={"paths": wanted})
        with tempfile.TemporaryDirectory(prefix="genus-manifest-context-") as scratch:
            root = Path(scratch)
            manifest_dir = root / "docs" / "agents"
            manifest_dir.mkdir(parents=True)
            if body.get("defaults_yaml"):
                write_atomically(manifest_dir / "_defaults.yaml", str(body["defaults_yaml"]))
            if body.get("schema_yaml"):
                write_atomically(manifest_dir / "schema.yaml", str(body["schema_yaml"]))
            resolved_root = root.resolve()
            for relative in body.get("existing") or []:
                if relative not in wanted:
                    continue
                target = (root / relative).resolve()
                if not target.is_relative_to(resolved_root):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            yield manifest_dir, root


Backend = LocalBackend | RemoteBackend


def is_platform(tenant: str | None) -> bool:
    return tenant is None or tenant == _operator.PLATFORM_TENANT


def backend_for(tenant: str | None) -> Backend:
    """The platform tenant's files directly; any other tenant's through its engine."""
    if is_platform(tenant):
        return LocalBackend()
    return RemoteBackend(str(tenant))


def scan_from_worker_thread(tenant: str | None) -> Any:
    """The tenant's manifest scan, for a SYNC handler (a worker thread).

    ``fleet`` and ``automations`` are plain ``def`` routes because psycopg2 is
    synchronous. The platform tenant's scan is a local read; a child tenant's
    is an engine call, run on the request's event loop via ``anyio.from_thread``.
    """
    if is_platform(tenant):
        return LocalBackend().scan_sync()
    import anyio.from_thread

    return anyio.from_thread.run(RemoteBackend(str(tenant)).scan)
