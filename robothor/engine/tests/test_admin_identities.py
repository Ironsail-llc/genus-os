"""Dropping the engine's identity caches from outside the engine.

The binding is a row, and the bridge or the CLI writes it from their own process
perfectly well. What they cannot do from there is make the ENGINE stop believing
the old answer: ``identity.resolvers`` caches a resolution for 60 s and
``engine.users`` caches a ``tenant_users`` row for **300 s**, both process-local.
So before this route an operator revoked a binding from the Helm, watched it
succeed, and the revoked sender went on driving the agent for up to five minutes.

Two properties, and the second is the one a green test can hide:

* it clears **both** caches, named rather than "the identity cache" — they have
  different TTLs and different keys, and clearing one while believing it was both
  is how a revoke works on Slack and not on Telegram;
* it is mounted under ``/api/admin``, which is what makes ``engine:control`` the
  scope required to call it.
"""

from __future__ import annotations

from robothor.engine.admin_identities import reload_identity_caches


def test_it_clears_the_resolver_cache():
    from robothor.identity import resolvers

    resolvers._cache[("slack", "U0PLACEHOLDER", "default")] = (None, float("inf"))

    reload_identity_caches()

    assert resolvers._cache == {}


def test_it_clears_the_tenant_users_cache():
    """The 300s one. Telegram resolves through ``lookup_user``, so a revoke that
    cleared only the resolver cache would work on Slack and silently not on
    Telegram -- the surface with the longer TTL and the owner role."""
    from robothor.engine import users

    users._cache[("100000001", "default")] = ({"role": "owner"}, float("inf"))

    reload_identity_caches()

    assert users._cache == {}


def test_it_names_both_caches_in_what_it_returns():
    """The response is what a caller logs when something looks stale, so it says
    which caches were actually dropped rather than just "ok"."""
    result = reload_identity_caches()

    assert result["reloaded"] is True
    assert set(result["caches"]) == {"identity_resolvers", "tenant_users"}


def test_the_route_is_mounted_under_the_admin_prefix():
    """``engine/auth.py`` gates ``engine:control`` by PREFIX, so a route mounted
    anywhere else would be reachable with a read-scoped dashboard token."""
    from fastapi import FastAPI

    from robothor.engine.admin_identities import register

    app = FastAPI()
    register(app)

    # FastAPI keeps an included router as a lazy group rather than flattening
    # its routes into ``app.routes``, so walk into it -- the same shape
    # ``crm/bridge/tests/test_route_concurrency.py`` has to handle.
    paths = {
        getattr(candidate, "path", "")
        for outer in app.routes
        for candidate in getattr(getattr(outer, "original_router", None), "routes", (outer,))
    }
    assert "/api/admin/identities/reload" in paths


def test_the_cli_client_posts_to_exactly_that_path():
    """The CLI's poster hardcodes one literal path -- its narrowness is the
    security property, since there is then no caller-supplied path to validate.
    This pins the two halves to the same string."""
    from robothor.engine.admin_client import IDENTITY_RELOAD_PATH

    assert IDENTITY_RELOAD_PATH == "/api/admin/identities/reload"
