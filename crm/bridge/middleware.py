"""Bridge middleware — correlation IDs, authentication, tenant isolation, RBAC, in one ASGI layer."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from robothor.audit.logger import log_event

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from robothor.auth.deps import AuthContext
from robothor.events.capabilities import check_endpoint_access, load_capabilities

# Load the agent capabilities manifest once at import time
load_capabilities()

logger = logging.getLogger(__name__)


def _log_unauthorized(request: Request, reason: str) -> None:
    """One WARNING per 401 that names the caller, so a rejected local client
    can be attributed from the journal (production showed repeated loopback
    401s with no way to tell which client held the stale token).

    Identity only — User-Agent and remote address. Never the credential.
    """
    client = request.client
    remote_addr = client.host if client is not None else "-"
    logger.warning(
        "401 %s %s from %s ua=%r: %s",
        request.method,
        request.url.path,
        remote_addr,
        request.headers.get("user-agent", "-"),
        reason,
    )


#: How long the known-agent set is trusted before it is rebuilt. Short on
#: purpose: it exists to stop a rebuild on EVERY request, not to hide a change.
KNOWN_AGENTS_TTL_SECONDS = 5.0

_known_agents_lock = threading.Lock()
#: ``(capabilities manifest the set was built from, built at, the set)``.
_known_agents: tuple[object, float, frozenset[str]] | None = None


def _known_agent_ids() -> frozenset[str]:
    """``set(list_agents())``, rebuilt at most once per TTL.

    Keyed on the capabilities manifest object as well as the clock, so a
    ``load_capabilities()`` or ``reset()`` is seen on the very next request
    rather than up to a TTL later.
    """
    global _known_agents
    from robothor.events import capabilities

    source = capabilities._get_manifest()
    now = time.monotonic()
    with _known_agents_lock:
        cached = _known_agents
        if (
            cached is not None
            and cached[0] is source
            and now - cached[1] < KNOWN_AGENTS_TTL_SECONDS
        ):
            return cached[2]
    agents = frozenset(capabilities.list_agents())
    with _known_agents_lock:
        _known_agents = (source, now, agents)
    return agents


def invalidate_known_agents() -> None:
    """Forget the known-agent set; agent install, update and remove call this."""
    global _known_agents
    with _known_agents_lock:
        _known_agents = None


def _with_header(send: Send, name: str, value: str) -> Send:
    """``send`` with one response header set (replacing any the app set),
    exactly as ``response.headers[name] = value`` did in a BaseHTTPMiddleware."""

    async def _send(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message)[name] = value
        await send(message)

    return _send


class BridgeMiddleware:
    """Correlation id, authentication, tenant scope, tenant binding and RBAC: one ASGI layer.

    These were four ``BaseHTTPMiddleware`` classes, and each one wrapped every
    request -- and every streamed body -- in a task and a memory stream of its
    own. This is the same four steps in the same order, as plain ASGI:

    1. **Correlation.** ``X-Correlation-Id`` is taken from the request or
       generated, put on ``request.state.correlation_id``, and set on EVERY
       response, the 401s and 403s below included.
    2. **Authentication.** ``request.state.auth`` is the verified bridge token
       or ``None``. The Instantly webhook POST and ``/api/setup/`` skip token
       verification (steps 3 and 4 still run). A supplied invalid token is a
       401 on any route; no token on a private route is a 401 when auth is
       enforced; a verified identity without the route's scope or role is a
       403.
    2b. **Tenant scope.** Everything after authentication runs inside
       ``tenant_scope(auth.tenant_id)`` (``_tenant_scoped``), so RLS binds each
       pooled connection to the ACTING tenant.
    3. **Tenant.** A verified token's tenant is the tenant; an ``X-Tenant-Id``
       that disagrees is a 403. In loopback dev mode the header is trusted.
       ``request.state.tenant_id`` is set, and ``X-Tenant-Id`` goes on the
       response (RBAC's 403 included) whenever there is a verified identity or
       dev mode is on -- never on a step-2 or step-3 refusal.
    4. **RBAC.** ``request.state.actor_id`` and ``agent_id`` are set; an
       ``X-Agent-Id`` that disagrees with the token, a service token naming an
       agent the capabilities manifest does not know, or an agent outside its
       ``bridge_endpoints`` is a 403.

    Every refusal is the same ``{"error": ...}`` JSON body and the same audit
    event it was before. Non-HTTP scopes (lifespan, websocket) pass straight
    through, as they did past ``BaseHTTPMiddleware``. Exceptions from the app
    propagate unchanged to the server-error layer outside this one.
    """

    _PUBLIC_PATHS = frozenset(
        {
            "/live",
            "/liveness",
            "/ready",
            "/api/auth/sso",
            "/api/auth/refresh",
            "/api/auth/logout",
            # Local email+password sign-in. Both must answer an unauthenticated
            # caller by definition — /methods tells the sign-in page which form
            # to draw, and /login is where a session comes from. Neither widens
            # anything else: both handlers 404 unless GENUS_LOCAL_LOGIN=true,
            # /login is rate limited per (email, IP) and lockout-guarded, and
            # /methods returns only names and booleans.
            "/api/auth/methods",
            "/api/auth/login",
        }
    )

    #: First-run setup. Public AND exempt from token verification, which is one
    #: step further than the routes above and needs its own justification.
    #:
    #: The wizard authenticates with a SETUP CLAIM token — ``typ: "setup"``,
    #: audience ``genus-setup`` — and it carries it in ``Authorization:
    #: Bearer``, the same header a session uses. ``verify_token`` rejects that
    #: type by design, so without this exemption the middleware would answer
    #: every wizard call with a 401 before the router ever saw the claim.
    #:
    #: The exemption is therefore not a hole: ``routers/setup.py`` verifies the
    #: claim itself on every route but ``status`` and ``claim``, and the whole
    #: router answers 404 once an owner account exists. Two tests keep it
    #: honest -- one asserting nothing else is ever mounted under this prefix,
    #: and one asserting an operator's own SESSION token is refused here.
    _SETUP_PREFIX = "/api/setup/"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        # 1. Correlation — outermost, so every answer below carries it.
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        send = _with_header(send, "X-Correlation-Id", correlation_id)

        # 2. Authentication.
        refusal = await self._authenticate(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        # 2b. Tenant scope: everything after authentication runs bound to the
        # token's tenant for RLS (``_tenant_scoped``).
        with self._tenant_scoped(request.state.auth):
            await self._after_auth(request, scope, receive, send)

    async def _after_auth(
        self, request: Request, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Steps 3 and 4 and the app, inside the request's tenant scope."""
        # 3. Tenant.
        tenant_id, refusal = self._bind_tenant(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        if request.state.auth is not None or _legacy_headers_allowed():
            send = _with_header(send, "X-Tenant-Id", tenant_id)

        # 4. RBAC — inside the tenant step, so its 403 carries X-Tenant-Id.
        refusal = self._authorize_agent(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _authenticate(self, request: Request) -> Response | None:
        """Verify a bridge-issued session/service token into ``request.state.auth``.

        - valid token  → ``request.state.auth = AuthContext`` (tenant comes from the token).
        - missing token on a private route → 401.
        - valid token without route scope/role → 403.
        - only probe and token bootstrap/rotation routes are public.
        """
        from robothor.auth.deps import token_from_request, verify_token
        from robothor.auth.tokens import TokenError
        from robothor.sales.ingestion import is_instantly_webhook

        request.state.auth = None
        # This exact POST authenticates its custom provider header in the route.
        # Do not exempt neighboring resources or other HTTP methods from JWTs.
        if is_instantly_webhook(request.method, request.url.path):
            return None
        if request.url.path.startswith(self._SETUP_PREFIX):
            return None

        token = token_from_request(request)
        if token:
            try:
                request.state.auth = verify_token(token, expected_audience="genus-bridge")
            except TokenError as e:
                # Always reject a supplied invalid credential, including on a
                # public route. Never expose PyJWT's distinguishing error text.
                log_event(
                    "auth.denied",
                    "invalid bridge token",
                    details={
                        "reason": str(e),
                        "method": request.method,
                        "path": request.url.path,
                    },
                    status="denied",
                )
                _log_unauthorized(request, "invalid or expired token")
                return JSONResponse(status_code=401, content={"error": "invalid or expired token"})

        verified = request.state.auth
        if verified is not None:
            refusal = await _session_refusal(verified)
            if refusal is not None:
                status, error = refusal
                log_event(
                    "auth.denied",
                    error,
                    actor=verified.actor_id,
                    details={"method": request.method, "path": request.url.path},
                    status="denied",
                )
                if status == 401:
                    _log_unauthorized(request, error)
                return JSONResponse(status_code=status, content={"error": error})

        path = request.url.path
        if _auth_enforced() and request.state.auth is None and path not in self._PUBLIC_PATHS:
            log_event(
                "auth.denied",
                f"Unauthenticated {request.method} {path}",
                details={"method": request.method, "path": path},
                status="denied",
            )
            _log_unauthorized(request, "authentication required")
            return JSONResponse(status_code=401, content={"error": "authentication required"})

        auth = request.state.auth
        if auth is not None:
            denial = _authorization_denial(auth, request.method, path)
            if denial:
                return _deny(request, denial, actor=auth.actor_id, public_error=denial)
        return None

    @staticmethod
    @contextmanager
    def _tenant_scoped(auth: AuthContext | None) -> Iterator[None]:
        """Bind the database scope to an authenticated request's ACTING tenant.

        Every pooled connection checked out beneath it binds ``app.tenant_id``
        to that tenant, and the migration-081 policy refuses another tenant's
        rows even where a query forgot its WHERE. The scope is a ContextVar;
        anyio copies the context into the threadpool, so sync handlers see it
        too. An unauthenticated request gets no scope, and with
        ``ROBOTHOR_RLS_ENABLED`` unset nothing binds at all. A human caller's
        audit rows written without an explicit actor (the DAL's CRM audit) are
        filed under that account, in the acting tenant, with the home tenant in
        their details.
        """
        tenant_id = getattr(auth, "tenant_id", None) if auth is not None else None
        if not tenant_id:
            yield
            return

        from robothor.audit.logger import request_actor
        from robothor.db.connection import tenant_scope

        if getattr(auth, "is_service", True):
            with tenant_scope(str(tenant_id)):
                yield
            return
        with (
            tenant_scope(str(tenant_id)),
            request_actor(
                user_id=str(auth.user_id),
                home_tenant_id=str(auth.home_tenant_id),
                tenant_id=str(tenant_id),
            ),
        ):
            yield

    @staticmethod
    def _bind_tenant(request: Request) -> tuple[str, Response | None]:
        """Derive the tenant from verified claims (or explicit loopback dev mode)."""
        from robothor.constants import DEFAULT_TENANT

        auth = request.state.auth
        requested_tenant = request.headers.get("x-tenant-id")
        if auth is not None:
            tenant_id = auth.tenant_id
            if requested_tenant and requested_tenant != tenant_id:
                log_event(
                    "auth.denied",
                    "tenant header does not match verified token",
                    actor=auth.actor_id,
                    details={"method": request.method, "path": request.url.path},
                    status="denied",
                )
                return tenant_id, JSONResponse(
                    status_code=403, content={"error": "tenant not authorized"}
                )
        elif _legacy_headers_allowed():
            tenant_id = requested_tenant or DEFAULT_TENANT
        else:
            # Only public endpoints can reach this branch in secure mode.
            tenant_id = DEFAULT_TENANT

        request.state.tenant_id = tenant_id
        return tenant_id, None

    @staticmethod
    def _authorize_agent(request: Request) -> Response | None:
        """Check capabilities for a verified service-agent identity.

        The legacy header is consulted only in explicit insecure development
        mode. A signed service identity must name a configured agent; unknown
        agents are denied even when an old capability manifest's default
        policy is ``allow``.
        """
        auth = request.state.auth
        requested_agent = request.headers.get("x-agent-id")
        agent_id: str | None = None

        if auth is not None:
            request.state.actor_id = auth.actor_id
            agent_id = auth.agent_id if auth.is_service else None
            if requested_agent and requested_agent != agent_id:
                return _deny(
                    request,
                    "agent header does not match verified token",
                    actor=auth.actor_id,
                    public_error="agent identity not authorized",
                )
            if auth.is_service and agent_id not in _known_agent_ids():
                return _deny(
                    request,
                    "verified token names an unknown agent",
                    actor=auth.actor_id,
                    public_error="agent not authorized",
                )
        elif _legacy_headers_allowed():
            agent_id = requested_agent
            request.state.actor_id = requested_agent

        request.state.agent_id = agent_id
        if agent_id:
            method = request.method
            path = request.url.path
            if not check_endpoint_access(agent_id, method, path):
                return _deny(
                    request,
                    f"Agent '{agent_id}' denied {method} {path}",
                    actor=agent_id,
                    public_error=f"Agent '{agent_id}' not authorized for {method} {path}",
                )
        return None


async def _session_refusal(auth: AuthContext) -> tuple[int, str] | None:
    """Why a verified token may not be honoured right now, or None.

    A signature proves who minted the token, not that what it says is still
    true. Two things can stop being true before it expires:

    * the switch was turned off — an ACTING token is honoured only while
      ``GENUS_TENANT_SWITCH_MODE`` is ``enforce`` (403), so turning it off ends
      every acting session at once;
    * the account moved tenant or was disabled — a HUMAN token is honoured
      only while its account still lives in ``home_tid`` and is active (401;
      ``robothor.auth.session_standing``, cached ~30 s per user).
    """
    from robothor.auth import session_standing
    from robothor.auth.runtime import tenant_switch_mode

    if auth.is_acting and tenant_switch_mode() != "enforce":
        return 403, "tenant switching disabled"
    if not auth.is_service and not await asyncio.to_thread(
        session_standing.stands, auth.user_id, auth.home_tenant_id
    ):
        return 401, "session no longer valid"
    return None


def _auth_enforced() -> bool:
    from robothor.auth.runtime import auth_required

    return auth_required(bind_host=_bridge_bind_host())


def _legacy_headers_allowed() -> bool:
    from robothor.auth.runtime import legacy_headers_allowed

    return legacy_headers_allowed(bind_host=_bridge_bind_host())


def _bridge_bind_host() -> str:
    return os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1")


_ACCESSIBLE_TENANTS_PATH = "/api/tenants/accessible"


def _authorization_denial(auth: AuthContext, method: str, path: str) -> str | None:
    """Return a stable public denial reason for a verified identity."""
    if path.startswith("/api/auth/"):
        return None

    required_scope = (
        "bridge:read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "bridge:write"
    )
    if not auth.has_scope(required_scope):
        return "insufficient scope"

    # This console controls only the verified tenant's sales data. It is
    # human-only even when a service token carries broad bridge scopes.
    if (path == "/api/sales" or path.startswith("/api/sales/")) and (
        auth.is_service or auth.role not in {"owner", "admin"}
    ):
        return "human tenant operator required"

    if path == _ACCESSIBLE_TENANTS_PATH:
        # The tenant picker: what THIS human may switch into. Every human role
        # may ask (a single-tenant user gets only their home); an agent has no
        # picker and no business enumerating the hierarchy.
        return "human user required" if auth.is_service else None

    if path.startswith("/api/tenants"):
        if auth.is_service:
            if not auth.has_scope("tenant:admin"):
                return "tenant administration not authorized"
        elif auth.role not in {"owner", "admin"}:
            return "role not authorized"

    if path.startswith("/api/audit") or path.startswith("/api/telemetry"):
        if auth.is_service:
            if not auth.has_scope("audit:read"):
                return "audit access not authorized"
        elif auth.role not in {"owner", "admin", "auditor"}:
            return "role not authorized"

    # The generic bridge scopes above grant access to ordinary tenant data.
    # Sensitive credentials, appliance-global state, and authority-changing
    # operations require a narrower capability.  Owner/admin sessions retain
    # their operator access; every other human or service caller must carry the
    # explicit scope named here.  This prevents a default ``member`` token (or
    # a generic ``bridge:*`` service token) from silently becoming an
    # administrator.
    if path.startswith("/api/vault/"):
        return _privileged_access_denial(auth, "vault:read", "vault access not authorized")

    integration_scope = _integration_scope(method, path)
    if integration_scope:
        return _privileged_access_denial(
            auth,
            integration_scope,
            "integration access not authorized",
        )

    if path == "/api/installed-agents" or path.startswith("/api/installed-agents/"):
        from routers._operator import is_root

        # Installation state writes the bridge host's own workspace — the
        # platform tenant's fleet. Only a caller AT HOME in the platform tenant
        # may administer or inspect it; a root admin acting in a child is not.
        if not is_root(auth):
            return "appliance administration not authorized for tenant"
        scope = "agent:read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "agent:admin"
        return _privileged_access_denial(auth, scope, "agent administration not authorized")

    memory_scope = _memory_admin_scope(method, path)
    if memory_scope:
        denial = _privileged_access_denial(
            auth,
            memory_scope,
            "memory administration not authorized",
        )
        if denial:
            return denial

        # Ingestion is tenant-aware (``ingest_content(tenant_id=...)``); the
        # pipeline trigger is not: it runs the host's own ingestion scripts,
        # which read the platform tenant's sources. Run under a child's binding
        # they would copy the platform's data into the child, so it stays with
        # a caller at home in the platform tenant.
        if path.startswith("/api/memory/pipeline/trigger/"):
            from routers._operator import is_root

            if not is_root(auth):
                return "memory operation not authorized for tenant"

    if method.upper() == "POST" and path.startswith("/api/tasks/"):
        action = path.rsplit("/", 1)[-1]
        if action in {"approve", "reject", "answer"}:
            return _privileged_access_denial(
                auth,
                "task:approve",
                "task approval not authorized",
            )

    return None


def _privileged_access_denial(
    auth: AuthContext,
    scope: str,
    public_error: str,
    *,
    operator_roles: frozenset[str] = frozenset({"owner", "admin"}),
) -> str | None:
    """Require an explicit capability or a trusted human operator role."""
    if auth.has_scope(scope):
        return None
    if not auth.is_service and auth.role in operator_roles:
        return None
    return public_error


def _integration_scope(method: str, path: str) -> str | None:
    """Return the narrow scope for legacy integration routes, if any."""
    if path == "/resolve-contact" or path == "/log-interaction":
        return "integration:write"
    if path.startswith("/timeline/"):
        return "integration:read"
    return None


def _memory_admin_scope(method: str, path: str) -> str | None:
    """Classify memory endpoints that expose or mutate administrative state."""
    verb = method.upper()
    if path == "/api/memory/store":
        return "memory:write"
    if path == "/api/memory/stats" or path.startswith("/api/memory/pipeline/"):
        return "memory:admin"
    if path == "/api/memory/blocks" or path.startswith("/api/memory/blocks/"):
        return "memory:read" if verb in {"GET", "HEAD", "OPTIONS"} else "memory:write"
    return None


def _deny(
    request: Request,
    reason: str,
    *,
    actor: str,
    public_error: str,
) -> JSONResponse:
    log_event(
        "auth.denied",
        reason,
        actor=actor,
        details={"method": request.method, "path": request.url.path},
        status="denied",
    )
    return JSONResponse(status_code=403, content={"error": public_error})
