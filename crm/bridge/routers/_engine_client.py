"""One way for a bridge route to call the engine.

The split is decided in the design: real work stays in the engine behind
``/api/admin/*`` and the ``engine:control`` scope, and the bridge proxies. That
only holds if the proxying itself is one function — a second hand-rolled
``httpx`` call with its own token, timeout and error handling is how two halves
of the same appliance end up disagreeing about what an error looks like.

The credential is minted per call and lives seconds. It is a *service* token in
the engine's own audience: ``verify_engine_token`` refuses a bridge-audience
service token outright, so this is not a case of the bridge lending an agent
credential more authority than it had.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from robothor.auth.tokens import issue_service_token
from robothor.engine.auth import ENGINE_AUDIENCE
from routers._operator import PLATFORM_TENANT

logger = logging.getLogger(__name__)

#: How long a minted engine credential is good for. Long enough for one
#: request including a 20s test connection, short enough that a token captured
#: from a process listing is worthless by the time it is read.
TOKEN_TTL_SECONDS = 120

#: The service identity the engine's audit trail sees for these calls.
SERVICE_ID = "genus-bridge"

DEFAULT_TIMEOUT_SECONDS = 30.0


def engine_base_url() -> str:
    """Where the engine answers. Loopback unless deployment says otherwise."""
    explicit = os.environ.get("ROBOTHOR_ENGINE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")
    port = os.environ.get("ROBOTHOR_ENGINE_PORT", "18800")
    return f"http://{host}:{port}"


def _engine_token() -> str:
    return issue_service_token(
        SERVICE_ID,
        PLATFORM_TENANT,
        audience=ENGINE_AUDIENCE,
        scopes=("engine:control",),
        ttl_seconds=TOKEN_TTL_SECONDS,
    )


async def engine_request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Call one engine route, returning ``(status_code, decoded body)``.

    Never logs ``json``: the only bodies that travel this way carry a
    credential the operator has just typed, and a debug line is a permanent
    place for it. Failures are reported as a status and a fixed message rather
    than by re-raising, so a proxying route answers the browser with something
    actionable instead of a 500 and a traceback.
    """
    url = f"{engine_base_url()}{path}"
    headers = {"Authorization": f"Bearer {_engine_token()}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Engine call %s %s failed: %s", method, path, type(exc).__name__)
        return 502, {"error": "engine unavailable"}

    try:
        body = response.json()
    except ValueError:
        body = {"error": "engine returned a non-JSON response"}
    return response.status_code, body
