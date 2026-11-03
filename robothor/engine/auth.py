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
from robothor.auth.runtime import auth_required, tenant_switch_mode
from robothor.auth.tokens import TokenError

#: The answer to an acting token while tenant switching is not enforced.
SWITCHING_DISABLED = "tenant switching disabled"


class SwitchingDisabledError(TokenError):
    """An acting token arrived while ``GENUS_TENANT_SWITCH_MODE`` is not ``enforce``."""


def _acting_refused(context: AuthContext) -> bool:
    """Whether this is an acting token the switch flag no longer honours.

    The bridge refuses an acting token (``act: true``, ``tid != home_tid``)
    unless switching is enforced, and so must every engine: the dashboard
    forwards the user's own token to ``/chat/*``, and a child tenant's engine
    accepts a token for its own tenant whatever the token's home. Service
    tokens cannot act; home tokens are unaffected.
    """
    return context.is_acting and tenant_switch_mode() != "enforce"


if TYPE_CHECKING:
    from fastapi import Request, WebSocket

logger = logging.getLogger(__name__)

ENGINE_AUDIENCE = "genus-engine"
BRIDGE_AUDIENCE = "genus-bridge"

PROBE_PATHS = frozenset({"/live", "/liveness", "/ready", "/health/startup"})

_CONTROL_PATHS = (
    # The whole /api/admin surface, reads included: it enumerates which
    # credentials exist and dials them. A read-scoped dashboard token is the
    # wrong identity for that, and a route added under this prefix later must
    # inherit the requirement rather than have to remember it.
    re.compile(r"^/api/admin(?:/.*)?$"),
    re.compile(r"^/api/extensions/reload$"),
    re.compile(r"^/api/runs/active$"),
    re.compile(r"^/api/runs/[^/]+/(?:resume|steer|interrupt)$"),
    re.compile(r"^/api/agents/[^/]+/trigger$"),
    re.compile(r"^/api/workflows/[^/]+/execute$"),
    # A run's step detail carries every tool input and output the run saw.
    re.compile(r"^/api/workflows/runs/[^/]+$"),
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


def channel_uses_own_auth(method: str, path: str) -> bool:
    """Whether the route belongs to a mounted plugin channel that authenticates it.

    A plugin channel's senders (a storefront, an SMS provider, a support form)
    never hold an engine token; the channel proves each request itself, with
    its own HMAC, shared token or signed JWT. So a POST to
    ``/api/channels/<name>`` or below skips the engine bearer, but only when
    ``<name>``'s router is actually mounted in this process. Every other
    method, and any name that is not mounted, still needs the bearer.
    """

    if method.upper() != "POST":
        return False
    from robothor.engine.channels import routers

    match = re.fullmatch(re.escape(routers.CHANNEL_PATH_PREFIX) + r"/([^/]+)(/.*)?", path)
    if match is None or ".." in path.split("/"):
        return False
    return routers.is_mounted(match.group(1))


def authenticate_http_request(request: Request, *, tenant_id: str) -> AuthContext | None:
    """Authenticate one HTTP request, returning ``None`` for probes, HMAC webhooks and channels.

    Raises FastAPI ``HTTPException`` with stable, non-oracular messages.
    """

    from fastapi import HTTPException

    path = request.url.path
    if (
        path in PROBE_PATHS
        or webhook_uses_channel_auth(request.method, path)
        or channel_uses_own_auth(request.method, path)
    ):
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
    if _acting_refused(context):
        raise HTTPException(status_code=403, detail=SWITCHING_DISABLED)

    scope = required_scope(request.method, path)
    if not context.has_scope(scope):
        raise HTTPException(status_code=403, detail="insufficient scope")
    return context


#: The answer an engine route gives a human token whose account no longer stands.
SESSION_NO_LONGER_VALID = "session no longer valid"


def session_stands(context: AuthContext) -> bool:
    """Whether a verified identity's account still stands where its token says.

    The same check the bridge makes (``robothor.auth.session_standing``): a
    HUMAN token is honoured only while its account still lives in the token's
    home tenant and is active. The dashboard forwards the user's own bridge
    token to ``/chat/*`` and ``/api/dashboard/completions``, so without this a
    disabled or moved account kept engine chat until the token expired.

    Service tokens and the loopback-development identity are not accounts and
    are not looked up. Blocking (a SELECT on a cache miss): async callers run it
    with ``asyncio.to_thread``. The import is guarded so an engine without the
    module (a stubbed test environment) still serves.
    """
    if context.is_service or context.audience == "loopback-development":
        return True
    try:
        from robothor.auth import session_standing
    except ImportError:  # pragma: no cover - the module ships with the platform
        return True
    return session_standing.stands(context.user_id, context.home_tenant_id)


def require_standing(context: AuthContext | None) -> AuthContext:
    """``context``, or a 401: none verified, or its account no longer stands.

    Blocking, like :func:`session_stands`: a sync FastAPI dependency (which
    runs in the threadpool) calls it directly.
    """
    from fastapi import HTTPException

    if context is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if not session_stands(context):
        raise HTTPException(status_code=401, detail=SESSION_NO_LONGER_VALID)
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
    if _acting_refused(context):
        raise SwitchingDisabledError(SWITCHING_DISABLED)
    if not context.has_scope("engine:chat"):
        raise TokenError("insufficient scope")
    return context
