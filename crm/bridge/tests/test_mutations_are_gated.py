"""Program-level guard: no bridge mutation route ships ungated by accident.

A per-router test only proves the routers someone remembered to test.  This
test walks the *assembled* FastAPI app instead, so a new POST/PUT/PATCH/DELETE
fails the suite the moment it is added unless it either

  * calls ``require_operator(request)`` in its handler body, or
  * appears in ``JUSTIFIED_WITHOUT_OPERATOR_GATE`` below with a reason.

The allowlist is deliberately narrow and every entry names the *middleware*
check that already constrains the route (``crm/bridge/middleware.py``,
``_authorization_denial``).  "It has always been open" is not a reason; if a
route mutates appliance-global state or reads/writes credentials it belongs
behind the operator gate, not on this list.

Every mutation route is enumerated, not just the ones under ``/api/``: the
legacy integration webhooks (``POST /resolve-contact``, ``POST
/log-interaction``) are mounted at the root and would have walked straight
through an ``/api/``-only filter, as would the next route someone mounts
beside them.
"""

from __future__ import annotations

import inspect
from typing import Any

from bridge_service import app

MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Route-path prefix → the one-line reason it needs no operator gate.
#
# A prefix matches a route when the path equals it or continues with "/".
# Each reason cites the middleware clause that actually constrains the route;
# without such a clause the route must be gated instead of listed here.
JUSTIFIED_WITHOUT_OPERATOR_GATE: dict[str, str] = {
    "/api/auth": (
        "Public session bootstrap/rotation — AuthMiddleware._PUBLIC_PATHS lists "
        "/api/auth/sso|refresh|logout and _authorization_denial() returns None for "
        "/api/auth/*; requiring an operator session to *create* a session is circular."
    ),
    "/api/people": (
        "Tenant CRM data written by member sessions and agent tokens — "
        "_authorization_denial() requires the bridge:write scope and TenantMiddleware "
        "pins every row to the verified token's tenant."
    ),
    "/api/companies": (
        "Tenant CRM data written by member sessions and agent tokens — bridge:write "
        "scope + TenantMiddleware tenant pinning, same as /api/people."
    ),
    "/api/conversations": (
        "Tenant CRM data written by member sessions and agent tokens — bridge:write "
        "scope + TenantMiddleware tenant pinning, same as /api/people."
    ),
    "/api/notes": (
        "Tenant CRM data written by member sessions and agent tokens — bridge:write "
        "scope + TenantMiddleware tenant pinning, same as /api/people."
    ),
    "/api/notifications": (
        "Tenant CRM data written by member sessions and agent tokens — bridge:write "
        "scope + TenantMiddleware tenant pinning, same as /api/people."
    ),
    "/api/routines": (
        "Tenant-scoped scheduled work (every DAL call takes tenant_id) — bridge:write "
        "scope + TenantMiddleware tenant pinning, same as /api/people."
    ),
    "/api/tasks": (
        "Tenant CRM data under bridge:write; the authority-bearing actions "
        "(approve|reject|answer) are separately scope-gated by _authorization_denial() "
        "behind the narrow task:approve capability."
    ),
    # More specific first: _justification() returns the first prefix that
    # matches, and /api/memory would otherwise swallow /api/memory/search and
    # lend it a justification that does not apply to it.
    "/api/memory/search": (
        "A read (a POST only because the query goes in the body) that "
        "_memory_admin_scope() does NOT cover — it falls back to plain bridge:write, "
        "and is tenant-pinned by TenantMiddleware like any other tenant-data read."
    ),
    "/api/memory": (
        "Scope-gated by _memory_admin_scope() in _authorization_denial() — memory:write "
        "for store, memory:admin for stats/pipeline, memory:read|write for blocks — and "
        "store/pipeline additionally fail closed for any non-primary tenant. Does NOT "
        "cover /api/memory/search, which has its own entry above."
    ),
    "/api/tenants": (
        "Tenant administration is already role-gated by _authorization_denial(): a "
        "service caller needs the tenant:admin scope and a human must be owner/admin. "
        "It must stay reachable to another tenant's own owner, which require_operator "
        "(platform-tenant only) would forbid."
    ),
    # Root-mounted legacy integration webhooks — the reason this guard does not
    # filter on "/api/".
    "/resolve-contact": (
        "Legacy root-mounted integration webhook, scope-gated by _integration_scope() "
        "in _authorization_denial() behind the narrow integration:write capability; "
        "the resolved contact is written under the verified token's tenant."
    ),
    "/log-interaction": (
        "Legacy root-mounted integration webhook, scope-gated by _integration_scope() "
        "in _authorization_denial() behind the narrow integration:write capability; "
        "every DAL call it makes takes the verified tenant_id."
    ),
}

# Never allowlistable: these mutate credentials or appliance-global install
# state and must carry the operator gate in the handler itself.
MUST_BE_GATED_PREFIXES = ("/api/vault", "/api/installed-agents")


def _all_routes() -> list[Any]:
    """Every route of the assembled app, flattened.

    FastAPI >= 0.139 keeps included routers as ``_IncludedRouter`` entries in
    ``app.routes`` instead of flattening them, so a naive loop over
    ``app.routes`` sees four docs routes and nothing else — a guard test that
    passes because it inspected nothing.  ``iter_route_contexts`` resolves the
    effective path/methods/endpoint; older FastAPI keeps the flat list.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover — FastAPI < 0.139
        return list(app.routes)
    return list(iter_route_contexts(app.routes))


def _mutation_routes() -> list[Any]:
    """Every mutating route on the app — no path filter, deliberately."""
    routes = []
    for route in _all_routes():
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not (methods & MUTATION_METHODS):
            continue
        routes.append(route)
    return routes


def _matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _justification(path: str) -> str | None:
    for prefix, reason in JUSTIFIED_WITHOUT_OPERATOR_GATE.items():
        if _matches(path, prefix):
            return reason
    return None


def _describe(route: Any) -> str:
    methods = ",".join(sorted(set(route.methods) & MUTATION_METHODS))
    endpoint = route.endpoint
    return f"{methods} {route.path} -> {endpoint.__module__}.{endpoint.__name__}"


# Routers that demonstrably contribute mutation routes today. A *partial*
# enumeration collapse — one include_router shape this walk cannot follow —
# would keep the count respectable while quietly dropping a whole router, so
# the floor alone is not enough: name them.
EXPECTED_ROUTER_MODULES = frozenset(
    {
        "routers.installed_agents",
        "routers.controls",
        "routers.notes_tasks",
        "routers.routines",
        "routers.integration",
    }
)


def test_the_app_actually_exposes_mutation_routes() -> None:
    """Guard the guard: a partial enumeration would make every assertion vacuous."""
    routes = _mutation_routes()
    assert len(routes) >= 35, f"route enumeration collapsed — only found {len(routes)}"

    seen = {route.endpoint.__module__ for route in routes}
    missing = EXPECTED_ROUTER_MODULES - seen
    assert not missing, f"no mutation routes enumerated from {sorted(missing)}"


def test_every_mutation_route_is_operator_gated_or_justified() -> None:
    ungated = [
        _describe(route)
        for route in _mutation_routes()
        if "require_operator(" not in inspect.getsource(route.endpoint)
        and _justification(route.path) is None
    ]
    assert not ungated, (
        "Mutation routes with neither require_operator(request) nor a justified "
        "allowlist entry:\n  " + "\n  ".join(sorted(ungated))
    )


def test_credential_and_install_routes_are_never_allowlisted() -> None:
    for prefix in JUSTIFIED_WITHOUT_OPERATOR_GATE:
        for forbidden in MUST_BE_GATED_PREFIXES:
            assert not _matches(prefix, forbidden) and not _matches(forbidden, prefix), (
                f"{prefix!r} would allowlist {forbidden!r} — credential and appliance "
                "install routes must carry the operator gate"
            )


def test_install_state_mutations_are_gated_in_the_handler() -> None:
    """The specific regression this guard was written for."""
    install_routes = [
        route for route in _mutation_routes() if route.path.startswith("/api/installed-agents")
    ]
    assert len(install_routes) == 3, f"expected install/update/remove, got {install_routes}"
    for route in install_routes:
        assert "require_operator(" in inspect.getsource(route.endpoint), _describe(route)


def test_every_allowlist_entry_still_matches_a_live_route() -> None:
    """A stale exemption is a hole nobody is looking at."""
    paths = [route.path for route in _mutation_routes()]
    dead = [
        prefix
        for prefix in JUSTIFIED_WITHOUT_OPERATOR_GATE
        if not any(_matches(path, prefix) for path in paths)
    ]
    assert not dead, f"allowlist entries match no route any more: {dead}"


def test_every_allowlist_entry_cites_a_reason() -> None:
    for prefix, reason in JUSTIFIED_WITHOUT_OPERATOR_GATE.items():
        assert len(reason.strip()) > 40, f"{prefix} needs a real justification, not {reason!r}"
