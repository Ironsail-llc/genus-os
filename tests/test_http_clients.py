"""Shared, pooled HTTP clients for the engine's hot paths.

``call_service`` (bridge, vision and voice tools) and every local Ollama
request used to build a fresh ``httpx.AsyncClient`` per call, so each call paid
a new TCP connection and a new pool. These tests pin the shared-client
contract: one client per event loop, reused across calls; closed by the
engine's shutdown hook; rebuilt on a new loop; and the per-request timeouts
are exactly the ones each call used before.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from robothor.engine.tools import service_client
from robothor.llm import ollama

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _CountingFactory:
    """Stands in for ``httpx.AsyncClient``: records every client it builds."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.made: list[httpx.AsyncClient] = []
        self._transport = transport

    def __call__(self, *args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if self._transport is not None and kwargs.get("transport") is None:
            kwargs["transport"] = self._transport
        client = _REAL_ASYNC_CLIENT(*args, **kwargs)
        self.made.append(client)
        return client


def _ok_transport(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _fresh_state():
    service_client.reset_circuit_breakers()
    yield
    service_client.reset_circuit_breakers()
    ollama._transport = None


async def _close_shared_clients() -> None:
    """The engine's shutdown hook (imported late: it is what these tests specify)."""
    from robothor.http_clients import close_shared_clients

    await close_shared_clients()


# ── call_service ────────────────────────────────────────────────────────


async def test_call_service_reuses_one_client_across_calls(monkeypatch):
    factory = _CountingFactory(_ok_transport())
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    for _ in range(50):
        result = await service_client.call_service("bridge", "GET", "http://bridge.test/health")
        assert result == {"ok": True}

    assert len(factory.made) == 1
    await _close_shared_clients()


async def test_call_service_passes_the_callers_timeout_per_request(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(httpx, "AsyncClient", _CountingFactory(_ok_transport(seen)))

    await service_client.call_service("bridge", "GET", "http://bridge.test/a", timeout=7.5)
    await service_client.call_service("bridge", "GET", "http://bridge.test/b")

    assert seen[0].extensions["timeout"] == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}
    # The default is still 10 s.
    assert seen[1].extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    await _close_shared_clients()


async def test_shutdown_closes_the_shared_client_and_the_next_call_rebuilds(monkeypatch):
    factory = _CountingFactory(_ok_transport())
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    await service_client.call_service("bridge", "GET", "http://bridge.test/health")
    await _close_shared_clients()

    assert factory.made[0].is_closed
    await service_client.call_service("bridge", "GET", "http://bridge.test/health")
    assert len(factory.made) == 2
    assert not factory.made[1].is_closed
    await _close_shared_clients()


def test_a_new_event_loop_gets_a_new_client(monkeypatch):
    factory = _CountingFactory(_ok_transport())
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def two_calls() -> None:
        await service_client.call_service("bridge", "GET", "http://bridge.test/a")
        await service_client.call_service("bridge", "GET", "http://bridge.test/b")

    asyncio.run(two_calls())
    asyncio.run(two_calls())

    # One client per loop: reused within a loop, never carried into the next.
    assert len(factory.made) == 2
    assert factory.made[0] is not factory.made[1]


def test_engine_shutdown_path_closes_shared_clients():
    """daemon.py's shutdown awaits the shared-client close."""
    from pathlib import Path

    import robothor.engine

    daemon_source = (Path(robothor.engine.__file__).parent / "daemon.py").read_text()
    assert "await close_shared_clients()" in daemon_source


# ── Ollama ──────────────────────────────────────────────────────────────


def _ollama_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        return httpx.Response(
            200,
            json={"message": {"content": "hi", "thinking": ""}, "done_reason": "stop"},
        )

    return httpx.MockTransport(handler)


async def test_ollama_requests_share_one_client(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://ollama.test")
    ollama._transport = _ollama_transport(seen)
    factory = _CountingFactory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    await ollama.get_embedding_async("one")
    await ollama.get_embedding_async("two")
    await ollama.chat([{"role": "user", "content": "hi"}])
    await ollama.check_model_available("qwen3:8b")

    assert len(seen) == 4
    # Inference and the ungated probe each keep one pooled client, not one per call.
    assert len(factory.made) <= 2
    await _close_shared_clients()
    assert all(c.is_closed for c in factory.made)


async def test_ollama_timeouts_are_unchanged(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://ollama.test")
    ollama._transport = _ollama_transport(seen)

    await ollama.get_embedding_async("one")
    await ollama.chat([{"role": "user", "content": "hi"}])
    await ollama.check_model_available("qwen3:8b")

    by_path = {r.url.path: r.extensions["timeout"]["read"] for r in seen}
    assert by_path == {"/api/embed": 30.0, "/api/chat": 180.0, "/api/tags": 5.0}
    await _close_shared_clients()


# ── Real sockets: connection setup is paid once ─────────────────────────


class _CountingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = 0
    lock = threading.Lock()

    def setup(self) -> None:
        with _CountingHandler.lock:
            _CountingHandler.connections += 1
        super().setup()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def local_server():
    _CountingHandler.connections = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


async def test_sequential_calls_reuse_one_tcp_connection(local_server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    for _ in range(20):
        result = await service_client.call_service("bridge", "GET", f"{local_server}/health")
        assert result == {"ok": True}

    assert _CountingHandler.connections == 1
    await _close_shared_clients()


# ── Two live loops, and state that must not travel between callers ─────


async def test_a_second_live_loop_does_not_evict_this_loops_client(monkeypatch):
    """A worker thread's ``asyncio.run`` (rlm_tool, rerank_sync) runs while the
    engine loop is alive; each loop keeps its own client, and the engine's is
    neither replaced nor left unclosed."""
    factory = _CountingFactory(_ok_transport())
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    await service_client.call_service("bridge", "GET", "http://bridge.test/a")
    assert len(factory.made) == 1
    mine = factory.made[0]

    def worker() -> None:
        async def call() -> None:
            await service_client.call_service("bridge", "GET", "http://bridge.test/w")

        asyncio.run(call())

    await asyncio.to_thread(worker)
    await asyncio.to_thread(worker)

    await service_client.call_service("bridge", "GET", "http://bridge.test/b")
    # Still the same client on this loop: the worker loops did not evict it.
    assert factory.made.count(mine) == 1
    assert not mine.is_closed
    assert len(factory.made) == 3  # one here, one per worker loop
    await _close_shared_clients()
    assert mine.is_closed


async def test_shared_clients_never_carry_cookies_between_calls(monkeypatch):
    """Each call used to have a throwaway client; a shared one must not keep a
    cookie jar that one tenant's response fills and the next call sends."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"ok": True}, headers={"Set-Cookie": "session=alice; Path=/"}
        )

    monkeypatch.setattr(httpx, "AsyncClient", _CountingFactory(httpx.MockTransport(handler)))

    await service_client.call_service("bridge", "GET", "http://bridge.test/a")
    await service_client.call_service("bridge", "GET", "http://bridge.test/b")

    assert "cookie" not in seen[1].headers
    await _close_shared_clients()
