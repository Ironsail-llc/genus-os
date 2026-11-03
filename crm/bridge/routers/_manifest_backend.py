"""The ACTING tenant's manifest scan, for the bridge's read-only fleet views.

One engine per tenant, each with its own manifest directory. The agent builder
(``agent_manifests``) forwards every call to the acting tenant's engine and
never reads a manifest itself. Two read-only views still scan the fleet in the
bridge — ``fleet`` and ``automations`` — and for them this module answers
"which manifests does the acting tenant have": the platform tenant's are read
through ``robothor.engine.manifest_store.scan`` on this host, a child tenant's
are asked of that tenant's own engine. A child tenant's admin is never shown
the root fleet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from robothor.engine import manifest_store
from routers import _operator
from routers._engine_client import engine_request

logger = logging.getLogger(__name__)

#: The child engine's raw scan. Under ``/api/admin``, so ``engine:control``.
SCAN_PATH = "/api/admin/manifests"


@dataclass
class Scan:
    """What a manifest directory holds: the readable documents and the failures.

    ``failures`` carry ``agent_id``, ``filename`` and ``error_type`` — the shape
    ``robothor.engine.manifest_rows._broken`` reads — and never a parser's detail.
    """

    manifests: list[dict[str, Any]]
    failures: list[Any] = field(default_factory=list)


def is_platform(tenant: str | None) -> bool:
    return tenant is None or tenant == _operator.PLATFORM_TENANT


async def remote_scan(tenant: str) -> Scan:
    """*tenant*'s engine's scan, over HTTP."""
    status, body = await engine_request("GET", SCAN_PATH, tenant=tenant)
    if status >= 400 or not isinstance(body, dict):
        logger.warning("A tenant engine's manifest scan answered %d", status)
        raise HTTPException(
            status_code=502, detail="the tenant's engine did not answer the manifest scan"
        )
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
    )


def scan_from_worker_thread(tenant: str | None) -> Any:
    """The tenant's manifest scan, for a SYNC handler (a worker thread).

    ``fleet`` and ``automations`` are plain ``def`` routes because psycopg2 is
    synchronous. The platform tenant's scan is a local read; a child tenant's
    is an engine call, run on the request's event loop via ``anyio.from_thread``.
    """
    if is_platform(tenant):
        return manifest_store.scan()
    import anyio.from_thread

    return anyio.from_thread.run(remote_scan, str(tenant))
