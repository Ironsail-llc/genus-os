"""Remote tool calls through the REAL bridge middleware and capabilities manifest.

``test_org_remote.py`` opens the capabilities step so it can test the route.
This file does not: the token is minted exactly as the home tool mints it
(``handlers.org_remote._headers``) for the agent ``main``, and the
capabilities manifest is a real file the middleware loads.

Nothing in the platform builds ``agent_capabilities.json`` from the agent
manifests: it is an instance file, and on a fresh install it does not exist.
So the remote routes are authorised by their own scope, ``org:remote``, which
only the engine's remote tools mint, and not by the agent being listed:

* no manifest at all: allowed;
* a manifest built from the shipped ``main`` template: allowed;
* a manifest that LISTS the agent without the two remote patterns: refused,
  naming the patterns to add (an operator who enumerates an agent's bridge
  endpoints means it);
* every other route keeps the old rule: an unknown agent is refused.

Hierarchy::

    acme-parent
    └── acme-child-a
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.tenancy.remote_contract import ORG_REMOTE_ENDPOINTS  # noqa: E402

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
ALICE = "11111111-1111-1111-1111-111111111111"
CALL = "/api/org/remote/call"
CATALOGUE = "/api/org/remote/catalogue"
TEMPLATE = Path(__file__).resolve().parents[3] / "templates/agents/core/main/manifest.template.yaml"


def _template_endpoints() -> list[str]:
    """``bridge_endpoints`` of the shipped ``main`` template (it is not YAML
    until rendered: take the one block)."""
    text = TEMPLATE.read_text()
    if "\nbridge_endpoints:\n" not in text:
        return []
    block = text.split("\nbridge_endpoints:\n", 1)[1].split("\n\n", 1)[0]
    return list(yaml.safe_load("bridge_endpoints:\n" + block)["bridge_endpoints"])


def _accessible(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {ROOT: (ROOT, CHILD_A), CHILD_A: (CHILD_A,)}[tenant]


async def _engine(method, path, *, json=None, timeout=30.0, tenant=None):
    return 200, {"outcome": "ok", "request_id": None, "tool": "t", "result": {}, "would": None}


@pytest.fixture
def capabilities(monkeypatch, tmp_path):
    """Point the middleware at a real manifest file (or none) and load it."""
    import middleware

    from robothor.events import capabilities as caps

    def _use(manifest: dict[str, Any] | None) -> None:
        path = tmp_path / "agent_capabilities.json"
        if manifest is not None:
            path.write_text(json.dumps(manifest))
        monkeypatch.setenv("ROBOTHOR_CAPABILITIES_MANIFEST", str(path))
        caps.reset()
        caps.load_capabilities()
        middleware.invalidate_known_agents()

    yield _use
    caps.reset()
    middleware.invalidate_known_agents()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.engine.tools.service_client import reset_bridge_token_cache
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    reset_settings()
    with (
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_accessible),
        patch("robothor.flags.store.resolve", side_effect=lambda name: None),
        patch("robothor.auth.acting.acting_role", return_value="owner"),
        patch("routers._engine_client.engine_request", new=_engine),
        patch("routers._audit.log_event"),
    ):
        yield
    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    reset_settings()


def _client():
    """A bridge client carrying the token the home tool mints for ``main``."""
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.org_remote import _headers

    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update(_headers(ToolContext(agent_id="main", tenant_id=ROOT)))
    return client


def _call(client):
    return client.post(
        CALL,
        json={
            "tenant": CHILD_A,
            "tool": "example_read",
            "args": {},
            "access": "read",
            "dry_run": False,
            "request_id": None,
            "requested_by": ALICE,
            "agent_id": "main",
        },
    )


def _catalogue(client):
    return client.get(CATALOGUE, params={"tenant": CHILD_A})


def test_the_shipped_main_template_declares_both_remote_patterns():
    assert set(ORG_REMOTE_ENDPOINTS) <= set(_template_endpoints())


def test_with_no_capabilities_manifest_the_remote_routes_answer(capabilities):
    capabilities(None)

    assert _call(_client()).status_code == 200
    assert _catalogue(_client()).status_code == 200


def test_with_a_manifest_built_from_the_shipped_template_they_answer(capabilities):
    capabilities({"agents": {"main": {"tools": [], "bridge_endpoints": _template_endpoints()}}})

    assert _call(_client()).status_code == 200
    assert _catalogue(_client()).status_code == 200


def test_a_manifest_listing_other_agents_only_still_lets_main_through(capabilities):
    capabilities({"agents": {"crm-steward": {"tools": [], "bridge_endpoints": ["GET /health"]}}})

    assert _call(_client()).status_code == 200


def test_an_agent_listed_without_the_patterns_is_refused_with_what_to_add(capabilities):
    capabilities({"agents": {"main": {"tools": [], "bridge_endpoints": ["GET /api/people/*"]}}})

    for response in (_call(_client()), _catalogue(_client())):
        assert response.status_code == 403
        error = response.json()["error"]
        for pattern in ORG_REMOTE_ENDPOINTS:
            assert pattern in error
        assert "bridge_endpoints" in error


def test_an_agent_listed_with_only_one_pattern_is_told_the_other(capabilities):
    capabilities({"agents": {"main": {"tools": [], "bridge_endpoints": ["GET /api/org/remote/*"]}}})

    assert _catalogue(_client()).status_code == 200
    response = _call(_client())
    assert response.status_code == 403
    assert "POST /api/org/remote/call" in response.json()["error"]
    assert "GET /api/org/remote/*" not in response.json()["error"]


def test_every_other_route_keeps_refusing_an_unknown_agent(capabilities):
    capabilities(None)

    response = _client().get("/api/people")

    assert response.status_code == 403
    assert response.json() == {"error": "agent not authorized"}


def test_a_token_without_the_scope_gets_nothing_from_the_exemption(capabilities):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    capabilities(None)
    token = tokens.issue_service_token("engine:main", ROOT, agent_id="main")
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})

    assert _call(client).status_code == 403
