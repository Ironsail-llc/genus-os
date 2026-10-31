"""The bridge's per-request overhead, and the behaviour that must survive cutting it.

Four costs were paid on every request and none of them bought anything:

* every engine call opened a brand-new ``httpx.AsyncClient`` (a new pool, a new
  connection) although the lifespan already holds a shared one;
* every ``GET /api/agent-manifests`` re-read and YAML-parsed the whole fleet,
  and the dashboard asks for it each time a message goes to a named agent;
* every service-token request rebuilt the known-agent set;
* four ``BaseHTTPMiddleware`` layers each wrapped every request and every
  streamed body in their own task.

The first half of this file pins the savings. The second half pins what the
four middleware layers did, in the order they did it, so collapsing them into
one cannot quietly change an answer, a header or a status code.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import yaml
from fastapi import (
    Request,  # noqa: TC002 — FastAPI resolves the probe route's annotation at runtime
)
from fastapi.responses import StreamingResponse

from robothor.auth import tokens

# ─── Shared fixtures ─────────────────────────────────────────────────


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _secure_mode(monkeypatch) -> None:
    """Authentication enforced, legacy headers off: the production posture."""
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")


# ─── Engine client: one pool, not one per call ───────────────────────


class TestTheEngineClientIsShared:
    @pytest.mark.asyncio
    async def test_two_engine_calls_use_the_lifespan_client(self, signing_key, monkeypatch):
        import bridge_service
        from routers import _engine_client

        seen: list[str] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        constructed: list[dict[str, Any]] = []
        real_client = httpx.AsyncClient

        def _spy(**kwargs):
            constructed.append(kwargs)
            return real_client(**kwargs)

        shared = real_client(transport=httpx.MockTransport(_handler), timeout=30.0)
        monkeypatch.setattr(bridge_service, "http_client", shared)
        monkeypatch.setattr(_engine_client.httpx, "AsyncClient", _spy)
        try:
            first = await _engine_client.engine_request("GET", "/api/admin/tools")
            second = await _engine_client.engine_request("POST", "/api/admin/scheduler/reconcile")
        finally:
            await shared.aclose()

        assert first == (200, {"ok": True})
        assert second == (200, {"ok": True})
        assert len(seen) == 2, "both calls must travel through the lifespan's client"
        assert constructed == [], "no per-call AsyncClient may be opened while one is shared"

    @pytest.mark.asyncio
    async def test_the_per_call_timeout_still_applies_on_the_shared_client(
        self, signing_key, monkeypatch
    ):
        import bridge_service
        from routers import _engine_client

        timeouts: list[dict[str, Any]] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(200, json={})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_handler), timeout=30.0)
        monkeypatch.setattr(bridge_service, "http_client", shared)
        try:
            await _engine_client.engine_request("GET", "/api/admin/tools", timeout=4.0)
        finally:
            await shared.aclose()

        assert timeouts[0]["read"] == 4.0

    @pytest.mark.asyncio
    async def test_a_dead_engine_on_the_shared_client_is_still_a_502(
        self, signing_key, monkeypatch
    ):
        import bridge_service
        from routers import _engine_client

        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
        monkeypatch.setattr(bridge_service, "http_client", shared)
        try:
            status, body = await _engine_client.engine_request("GET", "/api/admin/tools")
        finally:
            await shared.aclose()
        assert (status, body) == (502, {"error": "engine unavailable"})


# ─── Manifest listing: stat, do not re-parse ─────────────────────────


AGENT = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Does the demo work",
    "version": "2026-09-01",
    "department": "operations",
    "model": {"primary": "openrouter/example/demo-model"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["read_file"],
    "instruction_file": "brain/DEMO_AGENT.md",
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Three agents in a throwaway workspace (never the operator's — rule 11)."""
    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    for index in range(3):
        agent_id = f"demo-agent-{index}"
        document = {**AGENT, "id": agent_id, "name": f"Demo Agent {index}"}
        (manifests / f"{agent_id}.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return manifests


@pytest.fixture
def manifest_reads(monkeypatch):
    """Counts every manifest file the engine's loader opens."""
    from robothor.engine import config

    counter = {"files": 0, "defaults": 0}
    real_load = config._load_manifest_classified
    real_defaults = config._load_defaults

    def _counting_load(path, *args, **kwargs):
        counter["files"] += 1
        return real_load(path, *args, **kwargs)

    def _counting_defaults(*args, **kwargs):
        counter["defaults"] += 1
        return real_defaults(*args, **kwargs)

    monkeypatch.setattr(config, "_load_manifest_classified", _counting_load)
    monkeypatch.setattr(config, "_load_defaults", _counting_defaults)
    return counter


class TestTheManifestListingIsCached:
    def test_a_second_listing_reads_no_manifest_file(
        self, controls_client_as_operator, fleet, manifest_reads
    ):
        first = controls_client_as_operator.get("/api/agent-manifests")
        assert first.status_code == 200, first.text
        assert first.json()["count"] == 3
        assert manifest_reads["files"] == 3

        manifest_reads["files"] = manifest_reads["defaults"] = 0
        second = controls_client_as_operator.get("/api/agent-manifests")

        assert second.status_code == 200
        assert second.json() == first.json()
        assert manifest_reads == {"files": 0, "defaults": 0}

    def test_a_file_changed_on_disk_is_seen_by_the_next_listing(
        self, controls_client_as_operator, fleet, manifest_reads
    ):
        controls_client_as_operator.get("/api/agent-manifests")
        path = fleet / "demo-agent-1.yaml"
        document = yaml.safe_load(path.read_text())
        document["name"] = "Renamed Outside The Bridge"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        # A later mtime than the first read, however coarse the filesystem.
        later = time.time() + 5
        os.utime(path, (later, later))

        body = controls_client_as_operator.get("/api/agent-manifests").json()
        names = {row["id"]: row["name"] for row in body["agents"]}
        assert names["demo-agent-1"] == "Renamed Outside The Bridge"

    def test_a_file_added_or_removed_is_seen_by_the_next_listing(
        self, controls_client_as_operator, fleet
    ):
        assert controls_client_as_operator.get("/api/agent-manifests").json()["count"] == 3
        (fleet / "demo-agent-0.yaml").unlink()
        assert controls_client_as_operator.get("/api/agent-manifests").json()["count"] == 2
        document = {**AGENT, "id": "demo-agent-9", "name": "Demo Agent 9"}
        (fleet / "demo-agent-9.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        assert controls_client_as_operator.get("/api/agent-manifests").json()["count"] == 3

    def test_a_write_through_the_bridge_invalidates_even_when_mtimes_do_not_move(
        self, controls_client_as_operator, fleet, monkeypatch
    ):
        """The explicit invalidation, isolated from the stat signature.

        A filesystem with a coarse clock can give an edit the same mtime as the
        read before it; the bridge's own writes must not depend on the clock.
        Simulated by pinning the file back to its original mtime after the
        route has written it.
        """
        controls_client_as_operator.get("/api/agent-manifests")
        path = fleet / "demo-agent-2.yaml"
        before = path.stat()

        with patch(
            "routers.agent_manifests.engine_request",
            new=_engine_stub,
        ):
            response = controls_client_as_operator.post("/api/agent-manifests/demo-agent-2/disable")
        assert response.status_code == 200, response.text
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.utime(fleet, ns=(fleet.stat().st_atime_ns, fleet.stat().st_mtime_ns))

        body = controls_client_as_operator.get("/api/agent-manifests").json()
        row = next(row for row in body["agents"] if row["id"] == "demo-agent-2")
        assert row["enabled"] is False


async def _engine_stub(method, path, *, json=None, timeout=30):
    if path == "/api/admin/tools":
        return 200, {"tools": ["read_file"]}
    return 200, {"added": [], "replaced": [], "pruned": [], "blocked": {}, "clean": True}


# ─── Service-token RBAC: the known-agent set is not rebuilt per request ──


class TestTheKnownAgentSetIsCached:
    def _service_client(self, monkeypatch):
        from bridge_service import app
        from fastapi.testclient import TestClient

        monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
        token = tokens.issue_service_token(
            "email-classifier", "default", agent_id="email-classifier"
        )
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    @pytest.fixture
    def list_agents_calls(self, monkeypatch):
        from robothor.events import capabilities

        calls = {"n": 0}
        real = capabilities.list_agents

        def _counting():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(capabilities, "list_agents", _counting)
        return calls

    def test_ten_requests_ask_for_the_agent_list_once(
        self, signing_key, monkeypatch, list_agents_calls
    ):
        client = self._service_client(monkeypatch)
        for _ in range(10):
            assert client.get("/live").status_code == 200
        assert list_agents_calls["n"] == 1

    def test_an_install_or_remove_invalidates_the_set(
        self, signing_key, monkeypatch, list_agents_calls
    ):
        import middleware

        client = self._service_client(monkeypatch)
        client.get("/live")
        middleware.invalidate_known_agents()
        client.get("/live")
        assert list_agents_calls["n"] == 2

    def test_a_reloaded_capabilities_manifest_is_seen_at_once(
        self, signing_key, monkeypatch, list_agents_calls
    ):
        from robothor.events.capabilities import load_capabilities, reset

        client = self._service_client(monkeypatch)
        client.get("/live")
        reset()
        load_capabilities()
        client.get("/live")
        assert list_agents_calls["n"] == 2

    def test_the_set_expires_after_its_ttl(self, signing_key, monkeypatch, list_agents_calls):
        import middleware

        client = self._service_client(monkeypatch)
        client.get("/live")
        now = time.monotonic()
        monkeypatch.setattr(
            middleware.time, "monotonic", lambda: now + middleware.KNOWN_AGENTS_TTL_SECONDS + 1
        )
        client.get("/live")
        assert list_agents_calls["n"] == 2

    def test_an_unknown_agent_is_still_refused_from_the_cache(self, signing_key, monkeypatch):
        from bridge_service import app
        from fastapi.testclient import TestClient

        token = tokens.issue_service_token("ghost", "default", agent_id="ghost-agent")
        client = TestClient(app)
        for _ in range(2):
            response = client.get("/live", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403
            assert response.json() == {"error": "agent not authorized"}

    def test_the_installed_agents_routes_invalidate_the_set(self, monkeypatch):
        """install, update and remove all end by dropping the cached set and
        the cached manifest listing, whether or not the install succeeded."""
        import middleware
        from routers import agent_manifests, installed_agents

        dropped: list[str] = []
        monkeypatch.setattr(middleware, "invalidate_known_agents", lambda: dropped.append("agents"))
        monkeypatch.setattr(
            agent_manifests, "invalidate_manifest_listing", lambda: dropped.append("manifests")
        )

        async def _reconcile():
            return {"applied": True}

        monkeypatch.setattr(installed_agents, "reconcile_engine_schedules", _reconcile)
        monkeypatch.setattr(installed_agents, "require_operator", lambda request: None)
        monkeypatch.setattr(
            installed_agents, "_remove_installed", lambda agent_id, request: {"status": "removed"}
        )
        asyncio.run(installed_agents.remove_agent("demo-agent", request=None))  # type: ignore[arg-type]
        assert sorted(dropped) == ["agents", "manifests"]


# ─── One middleware, four behaviours, in order ───────────────────────


class TestOneMiddlewareLayer:
    def test_the_app_mounts_exactly_one_bridge_middleware(self):
        from bridge_service import app
        from starlette.middleware.base import BaseHTTPMiddleware

        assert len(app.user_middleware) == 1
        assert not any(issubclass(m.cls, BaseHTTPMiddleware) for m in app.user_middleware)  # type: ignore[arg-type]


class TestCorrelation:
    @pytest.mark.asyncio
    async def test_a_supplied_correlation_id_is_echoed(self, test_client):
        response = await test_client.get("/live", headers={"X-Correlation-Id": "qa-123"})
        assert response.headers["x-correlation-id"] == "qa-123"

    @pytest.mark.asyncio
    async def test_a_missing_correlation_id_is_generated(self, test_client):
        first = await test_client.get("/live")
        second = await test_client.get("/live")
        assert len(first.headers["x-correlation-id"]) == 32
        assert first.headers["x-correlation-id"] != second.headers["x-correlation-id"]

    @pytest.mark.asyncio
    async def test_a_401_still_carries_the_correlation_id(
        self, test_client, signing_key, monkeypatch
    ):
        _secure_mode(monkeypatch)
        response = await test_client.get("/api/people", headers={"X-Correlation-Id": "qa-401"})
        assert response.status_code == 401
        assert response.json() == {"error": "authentication required"}
        assert response.headers["x-correlation-id"] == "qa-401"
        assert "x-tenant-id" not in response.headers

    @pytest.mark.asyncio
    async def test_an_invalid_token_is_401_on_a_public_route_too(
        self, test_client, signing_key, monkeypatch
    ):
        _secure_mode(monkeypatch)
        response = await test_client.get(
            "/live",
            headers={"Authorization": "Bearer not-a-token", "X-Correlation-Id": "qa-bad"},
        )
        assert response.status_code == 401
        assert response.json() == {"error": "invalid or expired token"}
        assert response.headers["x-correlation-id"] == "qa-bad"

    @pytest.mark.asyncio
    async def test_the_route_sees_the_correlation_id_on_request_state(self, test_client):
        """``request.state`` set by the middleware reaches the route."""
        from bridge_service import app

        seen: dict[str, Any] = {}

        @app.get("/api/__hot_path_probe")
        async def _probe(request: Request) -> dict[str, str]:
            seen["correlation_id"] = request.state.correlation_id
            seen["tenant_id"] = request.state.tenant_id
            seen["agent_id"] = request.state.agent_id
            seen["auth"] = request.state.auth
            return {"ok": "yes"}

        try:
            response = await test_client.get(
                "/api/__hot_path_probe",
                headers={"X-Correlation-Id": "qa-state", "X-Tenant-Id": "tenant-dev"},
            )
        finally:
            app.router.routes.pop()

        assert response.status_code == 200
        assert seen == {
            "correlation_id": "qa-state",
            "tenant_id": "tenant-dev",
            "agent_id": None,
            "auth": None,
        }
        assert response.headers["x-correlation-id"] == "qa-state"
        assert response.headers["x-tenant-id"] == "tenant-dev"


class TestTenantBinding:
    @pytest.mark.asyncio
    async def test_a_mismatched_tenant_is_403_without_a_tenant_header(
        self, test_client, signing_key, monkeypatch
    ):
        _secure_mode(monkeypatch)
        token = tokens.issue_access_token("user-1", "tenant-a", "member")
        response = await test_client.get(
            "/live", headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": "tenant-b"}
        )
        assert response.status_code == 403
        assert response.json() == {"error": "tenant not authorized"}
        assert "x-tenant-id" not in response.headers
        assert "x-correlation-id" in response.headers

    @pytest.mark.asyncio
    async def test_the_verified_tenant_is_returned_and_bound(
        self, test_client, signing_key, monkeypatch
    ):
        _secure_mode(monkeypatch)
        token = tokens.issue_access_token("user-1", "tenant-a", "member")
        with patch("routers.people.list_people", return_value=[]) as list_people:
            response = await test_client.get(
                "/api/people", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        assert response.headers["x-tenant-id"] == "tenant-a"
        assert list_people.call_args.kwargs["tenant_id"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_no_tenant_header_is_set_for_an_anonymous_secure_request(
        self, test_client, monkeypatch
    ):
        _secure_mode(monkeypatch)
        response = await test_client.get("/live")
        assert response.status_code == 200
        assert "x-tenant-id" not in response.headers

    @pytest.mark.asyncio
    async def test_dev_mode_takes_the_tenant_from_the_header(self, test_client):
        response = await test_client.get("/live", headers={"X-Tenant-Id": "tenant-dev"})
        assert response.headers["x-tenant-id"] == "tenant-dev"


class TestRBAC:
    @pytest.mark.asyncio
    async def test_an_rbac_denial_carries_both_headers(self, test_client, signing_key, monkeypatch):
        """RBAC sat INSIDE the tenant layer, so its 403 got X-Tenant-Id too."""
        _secure_mode(monkeypatch)
        token = tokens.issue_service_token(
            "email-classifier", "tenant-a", agent_id="email-classifier"
        )
        response = await test_client.get(
            "/api/people",
            headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": "qa-rbac"},
        )
        assert response.status_code == 403
        assert response.json() == {
            "error": "Agent 'email-classifier' not authorized for GET /api/people"
        }
        assert response.headers["x-tenant-id"] == "tenant-a"
        assert response.headers["x-correlation-id"] == "qa-rbac"

    @pytest.mark.asyncio
    async def test_a_scope_denial_is_403_before_the_tenant_layer(
        self, test_client, signing_key, monkeypatch
    ):
        _secure_mode(monkeypatch)
        token = tokens.issue_service_token(
            "email-classifier", "tenant-a", agent_id="email-classifier", scopes=["bridge:read"]
        )
        response = await test_client.post(
            "/api/people", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403
        assert response.json() == {"error": "insufficient scope"}
        assert "x-tenant-id" not in response.headers

    @pytest.mark.asyncio
    async def test_a_denial_is_audited(self, test_client, signing_key, monkeypatch):
        _secure_mode(monkeypatch)
        token = tokens.issue_access_token("user-1", "tenant-a", "member")
        with patch("middleware.log_event") as log_event:
            await test_client.get(
                "/live", headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": "tenant-b"}
            )
        assert log_event.call_args.args[0] == "auth.denied"


class TestPassthrough:
    @pytest.mark.asyncio
    async def test_a_streamed_body_arrives_chunk_by_chunk(self):
        """Driven at the ASGI level: httpx's ASGITransport buffers the whole
        body, so it cannot tell one chunk from three."""
        from bridge_service import app

        async def _chunks():
            for index in range(3):
                yield f"chunk-{index}\n".encode()
                await asyncio.sleep(0)

        @app.get("/live/__hot_path_stream", response_model=None)
        async def _stream() -> StreamingResponse:
            return StreamingResponse(_chunks(), media_type="text/plain")

        sent: list[dict[str, Any]] = []
        requested = {"done": False}
        never = asyncio.Event()

        async def _receive() -> dict[str, Any]:
            # A server hands over the body once, then blocks until the client
            # goes away -- which this one never does.
            if not requested["done"]:
                requested["done"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await never.wait()
            return {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/live/__hot_path_stream",
            "raw_path": b"/live/__hot_path_stream",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test"), (b"x-correlation-id", b"qa-stream")],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        try:
            await app(scope, _receive, _send)
        finally:
            app.router.routes.pop()

        start = sent[0]
        assert start["type"] == "http.response.start"
        assert (b"x-correlation-id", b"qa-stream") in start["headers"]
        bodies = [m["body"] for m in sent[1:] if m["type"] == "http.response.body" and m["body"]]
        assert bodies == [b"chunk-0\n", b"chunk-1\n", b"chunk-2\n"]

    @pytest.mark.asyncio
    async def test_a_non_http_scope_passes_through_untouched(self):
        """Lifespan (and websocket) scopes are not requests: no headers, no auth."""
        import middleware

        seen: list[str] = []

        async def _inner(scope, receive, send):
            seen.append(scope["type"])

        layer = middleware.BridgeMiddleware(_inner)
        await layer({"type": "lifespan"}, None, None)
        assert seen == ["lifespan"]

    @pytest.mark.asyncio
    async def test_a_route_exception_still_reaches_the_exception_handlers(self, test_client):
        """An HTTPException is answered by FastAPI inside the middleware, so the
        error envelope gets the correlation header like any other response."""
        response = await test_client.get(
            "/api/agent-manifests/Not_A_Valid_Id", headers={"X-Correlation-Id": "qa-exc"}
        )
        assert response.headers["x-correlation-id"] == "qa-exc"
        assert response.status_code in {400, 401, 403, 404, 422}


# ─── The number the PR reports ───────────────────────────────────────


def test_per_request_overhead_on_a_no_op_route(capsys):
    """Not an assertion about speed: a printed figure for the PR.

    ``/live`` does no I/O, so what is measured is the middleware chain and the
    framework. Run with ``-s`` to see it.
    """
    from bridge_service import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    for _ in range(50):
        client.get("/live")
    runs = 500
    start = time.perf_counter()
    for _ in range(runs):
        client.get("/live")
    elapsed = time.perf_counter() - start
    with capsys.disabled():
        print(f"\n/live per-request: {elapsed / runs * 1e6:.0f} us over {runs} requests")
