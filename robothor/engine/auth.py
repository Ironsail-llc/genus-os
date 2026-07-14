"""Authentication and coarse authorization for the Agent Engine HTTP surface.

The Engine is a privileged execution service: a request that reaches chat or a
control endpoint can spend money, invoke tools, and mutate tenant data.  Network
location is therefore only a second line of defence.  Every non-probe request
must carry a signed Genus access token, except webhook ingress which performs
its own channel-specific HMAC verification.

Human dashboard sessions are Bridge-issued user tokens.  They are accepted only
when they explicitly carry an ``engine:*`` capability.  Workload tokens must be
minted for the dedicated ``genus-engine`` audience, preventing a generic Bridge
service token from being replayed against the Engine.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from robothor.auth.deps import AuthContext, bearer_from_header, verify_token
from robothor.auth.runtime import auth_required
from robothor.auth.tokens import TokenError

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

logger = logging.getLogger(__name__)

ENGINE_AUDIENCE = "genus-engine"
BRIDGE_AUDIENCE = "genus-bridge"

PROBE_PATHS = frozenset({"/live", "/liveness", "/ready", "/health/startup"})

_CONTROL_PATHS = (
    re.compile(r"^/api/extensions/reload$"),
    re.compile(r"^/api/runs/active$"),
    re.compile(r"^/api/runs/[^/]+/(?:resume|steer|interrupt)$"),
    re.compile(r"^/api/agents/[^/]+/trigger$"),
    re.compile(r"^/api/workflows/[^/]+/execute$"),
)


def _engine_bind_host() -> str:
    return os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")


def _development_context(tenant_id: str) -> AuthContext:
    """Return the explicit loopback-development identity.

    ``auth_required`` only returns false when ``GENUS_INSECURE_DEV_MODE`` is
    explicitly enabled, the process is not production, and the bind host is
    loopback.  Keeping the synthetic identity here means downstream code never
    has to reinterpret a missing role as a trusted system caller.
    """

    return AuthContext(
        user_id="loopback-development-operator",
        tenant_id=tenant_id,
        role="owner",
        typ="user",
        audience="loopback-development",
        scopes=frozenset({"engine:*"}),
        token_id="loopback-development",
    )


def verify_engine_token(token: str) -> AuthContext:
    """Verify a token that is valid for Engine use.

    Dedicated Engine tokens may represent a human or service.  Bridge-audience
    tokens are accepted only for human delegation from the dashboard and only
    when the token includes an explicit Engine capability.  A Bridge service
    credential can therefore never become an Engine execution credential.
    """

    try:
        return verify_token(token, expected_audience=ENGINE_AUDIENCE)
    except TokenError as engine_error:
        try:
            context = verify_token(token, expected_audience=BRIDGE_AUDIENCE)
        except TokenError:
            raise engine_error from None
        if context.is_service:
            raise TokenError("bridge service tokens are not valid for the engine") from engine_error
        if not any(scope == "engine:*" or scope.startswith("engine:") for scope in context.scopes):
            raise TokenError("bridge user token is not delegated to the engine") from engine_error
        return context


def required_scope(method: str, path: str) -> str:
    """Return the coarse Engine capability required for an HTTP route."""

    if any(pattern.fullmatch(path) for pattern in _CONTROL_PATHS):
        return "engine:control"
    if path.startswith("/chat/") or path == "/api/dashboard/completions":
        return "engine:read" if method.upper() in {"GET", "HEAD"} else "engine:chat"
    return "engine:read" if method.upper() in {"GET", "HEAD"} else "engine:write"


def webhook_uses_channel_auth(method: str, path: str) -> bool:
    """Whether the route performs HMAC authentication inside the handler."""

    return method.upper() == "POST" and bool(re.fullmatch(r"/api/webhooks/[^/]+", path))


def authenticate_http_request(request: Request, *, tenant_id: str) -> AuthContext | None:
    """Authenticate one HTTP request, returning ``None`` for public probes/HMAC webhooks.

    Raises FastAPI ``HTTPException`` with stable, non-oracular messages.
    """

    from fastapi import HTTPException

    path = request.url.path
    if path in PROBE_PATHS or webhook_uses_channel_auth(request.method, path):
        return None

    if not auth_required(bind_host=_engine_bind_host()):
        return _development_context(tenant_id)

    token = bearer_from_header(request.headers.get("authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        context = verify_engine_token(token)
    except TokenError as error:
        logger.info("Engine authentication rejected for %s: %s", path, type(error).__name__)
        raise HTTPException(status_code=401, detail="invalid or expired token") from error

    # The current Engine process owns one scheduler, workflow set, session
    # store, and configured tenant.  Until those appliance-global components
    # are partitioned, accepting a token for another tenant would expose the
    # configured tenant's run history and controls.  Deploy one Engine per
    # tenant and fail closed at this boundary.
    if context.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant not authorized for this engine")

    scope = required_scope(request.method, path)
    if not context.has_scope(scope):
        raise HTTPException(status_code=403, detail="insufficient scope")
    return context


def request_context(request: Request) -> AuthContext:
    """Return the identity installed by Engine middleware or fail closed."""

    from fastapi import HTTPException

    context = getattr(request.state, "auth", None)
    if isinstance(context, AuthContext):
        return context
    raise HTTPException(status_code=401, detail="authentication required")


def authenticate_websocket(websocket: WebSocket, *, tenant_id: str) -> AuthContext:
    """Authenticate an IDE WebSocket before accepting it."""

    if not auth_required(bind_host=_engine_bind_host()):
        return _development_context(tenant_id)

    token = bearer_from_header(websocket.headers.get("authorization"))
    if not token:
        raise TokenError("authentication required")
    context = verify_engine_token(token)
    if context.tenant_id != tenant_id:
        raise TokenError("tenant not authorized for this engine")
    if not context.has_scope("engine:chat"):
        raise TokenError("insufficient scope")
    return context
