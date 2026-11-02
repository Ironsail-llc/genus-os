"""The bridge's per-request overhead, and the behaviour that must survive cutting it.

Three costs were paid on every request and none of them bought anything:

* every engine call opened a brand-new ``httpx.AsyncClient`` (a new pool, a new
  connection) although the lifespan already holds a shared one;
* every service-token request rebuilt the known-agent set;
* four ``BaseHTTPMiddleware`` layers each wrapped every request and every
  streamed body in their own task.

The first half of this file pins the savings. The second half pins what the
four middleware layers did, in the order they did it, so collapsing them into
one cannot quietly change an answer, a header or a status code.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
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


def _share(monkeypatch, client: httpx.AsyncClient | None) -> None:
    """Install *client* as the lifespan's, bound to the running loop, as the
    lifespan itself does."""
    import bridge_service

    monkeypatch.setattr(bridge_service, "http_client", client)
    monkeypatch.setattr(
        bridge_service, "http_client_loop", asyncio.get_running_loop(), raising=False
    )


class TestTheEngineClientIsShared:
    @pytest.mark.asyncio
    async def test_two_engine_calls_use_the_lifespan_client(self, signing_key, monkeypatch):
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
        _share(monkeypatch, shared)
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
        from routers import _engine_client

        timeouts: list[dict[str, Any]] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(200, json={})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_handler), timeout=30.0)
        _share(monkeypatch, shared)
        try:
            await _engine_client.engine_request("GET", "/api/admin/tools", timeout=4.0)
        finally:
            await shared.aclose()

        assert timeouts[0]["read"] == 4.0

    @pytest.mark.asyncio
    async def test_a_dead_engine_on_the_shared_client_is_still_a_502(
        self, signing_key, monkeypatch
    ):
        from routers import _engine_client

        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
        _share(monkeypatch, shared)
        try:
            status, body = await _engine_client.engine_request("GET", "/api/admin/tools")
        finally:
            await shared.aclose()
        assert (status, body) == (502, {"error": "engine unavailable"})

    @pytest.mark.asyncio
    async def test_a_call_after_the_shared_client_closed_is_not_a_500(
        self, signing_key, monkeypatch
    ):
        """A closed lifespan client is no client: the call falls back to a
        one-off client instead of raising ``RuntimeError`` into a 500."""
        from routers import _engine_client

        # A mock transport, so the suite's network guard does not answer
        # before the closed client gets the chance to raise.
        closed = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        )
        await closed.aclose()
        _share(monkeypatch, closed)

        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            _engine_client.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(_refuse), **kwargs),
        )
        status, body = await _engine_client.engine_request("GET", "/api/admin/tools")
        assert (status, body) == (502, {"error": "engine unavailable"})

    @pytest.mark.asyncio
    async def test_a_call_from_another_event_loop_does_not_use_the_lifespan_client(
        self, signing_key, monkeypatch
    ):
        """An httpx client belongs to the loop it was opened on. A ``def``
        route that drives ``engine_request`` with ``asyncio.run`` in a worker
        thread runs on a second loop, and must get a client of its own."""
        from routers import _engine_client

        shared_calls: list[str] = []

        def _shared_handler(request: httpx.Request) -> httpx.Response:
            shared_calls.append(str(request.url))
            return httpx.Response(200, json={"via": "shared"})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_shared_handler))
        _share(monkeypatch, shared)

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            _engine_client.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"via": "own"})
                ),
                **kwargs,
            ),
        )
        try:
            answer = await asyncio.to_thread(
                lambda: asyncio.run(_engine_client.engine_request("GET", "/api/admin/tools"))
            )
        finally:
            await shared.aclose()

        assert answer == (200, {"via": "own"})
        assert shared_calls == [], "the lifespan's client was used from another loop"

    @pytest.mark.asyncio
    async def test_the_lifespan_forgets_its_client_on_shutdown(self, monkeypatch):
        import bridge_service

        from robothor.autonomy import handoff_worker

        async def _idle() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(bridge_service, "_routine_trigger_loop", _idle)
        monkeypatch.setattr(handoff_worker, "recover_checks", _idle)
        monkeypatch.setattr(handoff_worker, "purge_expired_observations", _idle)
        monkeypatch.setattr(bridge_service, "http_client", None)

        async with bridge_service.lifespan(bridge_service.app):
            assert bridge_service.http_client is not None
            assert bridge_service.http_client_loop is asyncio.get_running_loop()
        assert bridge_service.http_client is None
        assert bridge_service.http_client_loop is None

    @staticmethod
    def _flaky_shared(monkeypatch, error: Exception) -> list[str]:
        """A shared client whose first request fails with *error*, then answers."""

        attempts: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.method)
            if len(attempts) == 1:
                raise error
            return httpx.Response(200, json={"ok": True})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        _share(monkeypatch, shared)
        return attempts

    @pytest.mark.asyncio
    async def test_a_stale_pooled_connection_is_retried_once_for_a_read(
        self, signing_key, monkeypatch
    ):
        from routers import _engine_client

        attempts = self._flaky_shared(
            monkeypatch,
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
        )
        assert await _engine_client.engine_request("GET", "/api/admin/tools") == (
            200,
            {"ok": True},
        )
        assert attempts == ["GET", "GET"]

    @pytest.mark.asyncio
    async def test_a_dropped_post_is_not_replayed(self, signing_key, monkeypatch):
        """The engine may have acted on a POST before the connection dropped."""
        from routers import _engine_client

        attempts = self._flaky_shared(
            monkeypatch,
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
        )
        status, _ = await _engine_client.engine_request("POST", "/api/admin/scheduler/reconcile")
        assert status == 502
        assert attempts == ["POST"]

    @pytest.mark.asyncio
    async def test_a_refused_connect_is_retried_once_for_any_method(self, signing_key, monkeypatch):
        """A connect that failed sent nothing, so a retry cannot repeat a write."""
        from routers import _engine_client

        attempts = self._flaky_shared(monkeypatch, httpx.ConnectError("connection refused"))
        status, _ = await _engine_client.engine_request("POST", "/api/admin/scheduler/reconcile")
        assert status == 200
        assert attempts == ["POST", "POST"]


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

    def test_a_capabilities_manifest_that_failed_to_load_is_still_cached(
        self, signing_key, monkeypatch, tmp_path, list_agents_calls
    ):
        """A manifest that would not load is an empty manifest, and that is
        the same object on every request, so the cache still hits."""
        from robothor.events.capabilities import reset

        monkeypatch.setenv("ROBOTHOR_CAPABILITIES_MANIFEST", str(tmp_path / "absent.json"))
        reset()
        client = self._service_client(monkeypatch)
        for _ in range(3):
            assert client.get("/live").status_code == 403
        assert list_agents_calls["n"] == 1

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
        """install, update and remove all end by dropping the cached set,
        whether or not the install succeeded."""
        import middleware
        from routers import installed_agents

        dropped: list[str] = []
        monkeypatch.setattr(middleware, "invalidate_known_agents", lambda: dropped.append("agents"))

        async def _reconcile():
            return {"applied": True}

        monkeypatch.setattr(installed_agents, "reconcile_engine_schedules", _reconcile)
        monkeypatch.setattr(installed_agents, "require_operator", lambda request: None)
        monkeypatch.setattr(
            installed_agents, "_remove_installed", lambda agent_id, request: {"status": "removed"}
        )
        asyncio.run(installed_agents.remove_agent("demo-agent", request=None))  # type: ignore[arg-type]
        assert dropped == ["agents"]


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
