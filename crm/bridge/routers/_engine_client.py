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

Where the engine IS, and how the token is minted, moved to
``robothor.engine_control`` when the CLI and the doctor came to need the same
two answers. This module keeps what is specific to PROXYING: the path check
(these paths are assembled from ids a caller supplied) and the async client.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from robothor.engine_control import TOKEN_TTL_SECONDS as TOKEN_TTL_SECONDS
from robothor.engine_control import control_token
from robothor.engine_control import engine_base_url as _shared_base_url
from robothor.sanitize import sanitize_log
from routers._operator import PLATFORM_TENANT

logger = logging.getLogger(__name__)

#: The service identity the engine's audit trail sees for these calls.
SERVICE_ID = "genus-bridge"

DEFAULT_TIMEOUT_SECONDS = 30.0


#: The shape every engine path must have. The sibling of the scheme check in
#: ``robothor.engine_control.engine_base_url``, and newly load-bearing: paths used to be literals in this repo, and now the
#: agent-manifest routes assemble one from an agent id a caller supplied. A
#: path beginning ``//host`` or carrying a scheme, a query or a fragment would
#: move the request to a different server, or past the engine's own
#: ``/api/admin`` scope gate — neither of which is a thing a proxy should be
#: able to be talked into. One check at the sink, so no caller has to remember.
_SAFE_ENGINE_PATH = re.compile(r"/api/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?")


def _checked_path(path: str) -> str:
    """Return *path* if it is a literal engine API path, else refuse.

    ``.`` and ``..`` are rejected as whole segments and not left to the regex:
    they are spelled out of characters the regex allows, and httpx NORMALISES
    them before it dials — ``/api/admin/../../etc/passwd`` reaches the engine
    as ``/etc/passwd``, which is outside every path ``engine/auth.py`` scopes.

    Raises rather than answering the browser: every path here is constructed by
    this repo's own code, so a value that fails this is a programming error,
    and the same judgement ``engine_base_url`` already makes about a bad scheme.
    """
    if not _SAFE_ENGINE_PATH.fullmatch(path) or any(
        segment in {".", ".."} for segment in path.split("/")
    ):
        raise ValueError("engine path must be a literal /api/... path")
    return path


def engine_base_url() -> str:
    """Where the engine answers. Loopback unless deployment says otherwise.

    Shared with ``robothor.engine_control``, which the CLI and the doctor use:
    two answers to "where is the engine" is how a control call reaches a
    different instance than the one the operator is reading about.
    """
    return _shared_base_url()


def _engine_token() -> str:
    # ``require_existing_key=False``: the guard is for a CLI or a doctor on a
    # box whose vault may be unreadable, where minting would generate and
    # upsert a signing key. A bridge process that could not resolve the key
    # could not have verified the session of the caller it is proxying for, so
    # the check is pure latency here -- a live vault read per proxied request.
    return control_token(SERVICE_ID, PLATFORM_TENANT, require_existing_key=False)


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
    url = f"{engine_base_url()}{_checked_path(path)}"
    headers = {"Authorization": f"Bearer {_engine_token()}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        # ``path`` is sanitized because it is no longer always a literal: the
        # agent-manifest routes build one from an id the caller supplied, and a
        # newline in a log argument forges records.
        logger.warning(
            "Engine call %s %s failed: %s", method, sanitize_log(path), type(exc).__name__
        )
        return 502, {"error": "engine unavailable"}

    try:
        body = response.json()
    except ValueError:
        body = {"error": "engine returned a non-JSON response"}
    return response.status_code, body
