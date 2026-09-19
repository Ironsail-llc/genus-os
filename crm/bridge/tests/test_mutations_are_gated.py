"""Program-level guard: no bridge mutation route ships ungated by accident.

A per-router test only proves the routers someone remembered to test.  This
test walks the *assembled* FastAPI app instead, so a new POST/PUT/PATCH/DELETE
fails the suite the moment it is added unless it either

  * calls ``require_operator(request)`` in its handler body, or
  * appears in ``JUSTIFIED_WITHOUT_OPERATOR_GATE`` or the exact-method/path
    exception table below with a reason.

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

# Provider ingress has its own authentication rather than a human session.
# Exact method/path pairs are intentional: a prefix would exempt neighboring
# routes that AuthMiddleware's is_instantly_webhook() matcher does not exempt.
EXACT_JUSTIFIED_WITHOUT_OPERATOR_GATE = {
    ("POST", "/api/integrations/instantly/{tenant_id}/webhook"): (
        "AuthMiddleware delegates this exact POST to ingestion.webhook(), which "
        "compares the Authorization bearer header with the selected tenant's vault "
        "secret before reading the body. Workspace identity and owned campaign "
        "checks precede effects. robothor/sales/tests/test_ingestion.py covers "
        "missing/wrong secrets, tenant selection, payload bounds and ownership."
    ),
}


def _has_exact_justification(route) -> bool:
    methods = route.methods & MUTATION_METHODS
    return bool(methods) and all(
        (method, route.path) in EXACT_JUSTIFIED_WITHOUT_OPERATOR_GATE for method in methods
    )


def test_provider_ingress_justification_does_not_cover_neighbors_or_other_methods():
    from types import SimpleNamespace

    path = "/api/integrations/instantly/{tenant_id}/webhook"
    assert _has_exact_justification(SimpleNamespace(path=path, methods={"POST"}))
    assert not _has_exact_justification(SimpleNamespace(path=path + "/repair", methods={"POST"}))
    assert not _has_exact_justification(SimpleNamespace(path=path, methods={"DELETE"}))
    assert not _has_exact_justification(SimpleNamespace(path=path, methods={"POST", "DELETE"}))


# Route-path prefix → the one-line reason it needs no operator gate.
#
# A prefix matches a route when the path equals it or continues with "/".
# Each reason cites the middleware clause that actually constrains the route;
# without such a clause the route must be gated instead of listed here.
JUSTIFIED_WITHOUT_OPERATOR_GATE: dict[str, str] = {
    "/api/sales": (
        "Tenant-scoped human sales console: _authorization_denial() rejects services "
        "and non-owner/admin callers; require_sales_operator() repeats the human "
        "gate and derives every actor and tenant from verified auth. The platform-global "
        "require_operator() would incorrectly forbid another tenant's own sales team. "
        "Covered by robothor/sales/tests/test_api.py."
    ),
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
        "scope + TenantMiddleware tenant pinning, same as /api/people. The inbox "
        "READ is additionally caller-scoped in the handler (owner/admin any id, "
        "everybody else their own actor_id) because the webchat channel puts "
        "member-private chat deliveries in there; see "
        "test_notifications_inbox_scope.py."
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
    "/api/setup": (
        "First-run only, and require_operator is circular here in the strongest "
        "sense: these routes EXIST to create the operator, on a box that has no "
        "account, no issuer and no password. Two constraints stand in for the "
        "gate and both are enforced in routers/setup.py rather than by a "
        "middleware clause: every route calls _gate(), which 404s the moment "
        "robothor.setup_token.setup_complete() sees an owner row in the "
        "DATABASE, and every route but status/claim requires a setup CLAIM "
        "token (typ='setup', audience='genus-setup') that verify_token rejects "
        "and that a browser session can never be. The claim is bought once with "
        "the single-use token `genus init` printed, behind the same flood "
        "ceiling and a five-a-minute window. test_setup_routes.py parametrises "
        "the 404 over every route in the router and fails if one is added "
        "without coverage."
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
#
# ``/api/providers`` writes LLM credentials into the vault and rewrites the
# fleet's default model. Either one is enough on its own; a future "it is
# tenant data, bridge:write covers it" entry would hand every member session
# the ability to point the whole fleet at a model of their choosing.
#
# ``/api/agent-manifests`` writes the files that decide which agents exist, what
# model they dial, which tools they may call and when they fire. A member
# session that reached it could give itself an agent with `exec` on the
# appliance, so there is no tenant-scoping argument that could ever justify it.
MUST_BE_GATED_PREFIXES = (
    "/api/vault",
    "/api/installed-agents",
    "/api/providers",
    "/api/agent-manifests",
)


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
        # Credential mutations. Named here and not only counted: this router
        # carries the only routes in the appliance that write a provider key,
        # so a mount that silently stopped contributing routes would take the
        # gate assertions on them with it and still leave the floor intact.
        "routers.providers",
        # Manifest writes. Same reasoning: these are the only routes that decide
        # which agents exist and what they may do, and a mount that stopped
        # contributing them would leave both this floor and the per-route gate
        # assertions vacuously green.
        "routers.agent_manifests",
        # Channel pairing. The only network-reachable way to approve a pairing
        # -- i.e. to decide that a stranger who messaged the bot may drive it.
        # Named rather than only counted for the same reason as providers: if
        # this router silently stopped contributing routes, the gate assertions
        # on the approval endpoint would pass by being vacuous.
        "routers.channel_access",
        # Accounts and roles: the only network-reachable way to create an
        # account, change what it may do, or arm the one-shot grant that binds
        # it to an identity provider. Named for the same reason as the three
        # above — a mount that silently stopped contributing these routes would
        # leave every gate assertion in test_users_router.py vacuously green.
        "routers.users",
        # Forgetting a fact: the only network-reachable way to change what the
        # fleet believes. Named rather than only counted because /api/memory is
        # on the justification allowlist below — so if this router stopped
        # contributing routes, the prefix would keep the count honest while the
        # one gated route under it disappeared.
        "routers.memory_facts",
    }
)


def test_the_app_actually_exposes_mutation_routes() -> None:
    """Guard the guard: a partial enumeration would make every assertion vacuous."""
    # 35 -> 60 (actual 63) -> 66 with the three channel-access mutations
    # (approve, deny, revoke) -> 71 with the channel verify proxy and the three
    # account writes (invite, patch, arm a binding grant). The floor's whole job
    # is to notice an enumeration collapse, and one set 28 routes below reality
    # would have let nearly half the mutation surface disappear silently. Raise
    # it whenever routes are added, the same way the module ratchets are kept
    # tight. -> 73 with the memory forget preview and the forget itself.
    routes = _mutation_routes()
    assert len(routes) >= 73, f"route enumeration collapsed — only found {len(routes)}"

    seen = {route.endpoint.__module__ for route in routes}
    missing = EXPECTED_ROUTER_MODULES - seen
    assert not missing, f"no mutation routes enumerated from {sorted(missing)}"


def test_every_mutation_route_is_operator_gated_or_justified() -> None:
    ungated = [
        _describe(route)
        for route in _mutation_routes()
        if "require_operator(" not in inspect.getsource(route.endpoint)
        and _justification(route.path) is None
        and not _has_exact_justification(route)
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
    """The specific regression this guard was written for.

    Four now, not three: ``POST {id}/export`` joined them. It is a mutation
    route by shape rather than by effect — it writes nothing — but it carries
    the agent's instructions off the appliance, which is a bigger blast radius
    than the delete beside it, so it is held to the same gate rather than
    exempted for not being a write.
    """
    install_routes = [
        route for route in _mutation_routes() if route.path.startswith("/api/installed-agents")
    ]
    assert len(install_routes) == 4, f"expected install/update/remove/export, got {install_routes}"
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
