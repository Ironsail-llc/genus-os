"""MCP tool-list cache, parallel adapter connect and the shared HTTP client.

Behind ``ROBOTHOR_CONFIG_CACHE`` (default off). With it on:

* a run whose adapter session is still up re-uses the tool list the previous
  run fetched — zero ``tools/list`` calls — until that session drops, the
  plugin generation moves, or a caller asks for a refresh;
* adapters connect concurrently, each under its own timeout, so N adapters
  cost the slowest one rather than the sum and a hung one delays nobody;
* an HTTP MCP session keeps one ``httpx.AsyncClient`` instead of opening one
  per JSON-RPC request;
* the daemon pre-warms adapter sessions at start.

With it off every one of those is today's behaviour, and the tests below pin
that too.

Fakes sit at the transport seam: a stdio session subclass that never spawns a
process, and a fake ``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from robothor.engine import mcp_client
from robothor.engine.adapters import AdapterConfig

# ── fakes ────────────────────────────────────────────────────────────────


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 4242


class _Counters:
    """Shared across every fake session the pool creates in one test."""

    def __init__(self) -> None:
        self.list_calls: dict[str, int] = {}
        self.starts: dict[str, int] = {}
        self.latency: dict[str, float] = {}
        self.list_latency: dict[str, float] = {}


def _install_fake_stdio(monkeypatch: pytest.MonkeyPatch) -> _Counters:
    counters = _Counters()
    real = mcp_client.McpClientSession

    class _FakeStdioSession(real):  # type: ignore[misc, valid-type]
        async def start(self) -> None:
            name = self.config.name
            counters.starts[name] = counters.starts.get(name, 0) + 1
            delay = counters.latency.get(name, 0.0)
            if delay:
                await asyncio.sleep(delay)
            self._process = _FakeProcess()  # type: ignore[assignment]

        async def stop(self) -> None:
            self._process = None

        async def list_tools(self) -> list[dict[str, Any]]:
            name = self.config.name
            counters.list_calls[name] = counters.list_calls.get(name, 0) + 1
            delay = counters.list_latency.get(name, 0.0)
            if delay:
                await asyncio.sleep(delay)
            return [{"name": f"{name.replace('-', '_')}_tool", "description": "d"}]

    monkeypatch.setattr(mcp_client, "McpClientSession", _FakeStdioSession)
    return counters


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> mcp_client.McpClientPool:
    """A fresh singleton pool for each test."""
    fresh = mcp_client.McpClientPool()
    monkeypatch.setattr(mcp_client, "_pool", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _clear_backoff() -> Any:
    from robothor.engine.tools.registry import ToolRegistry

    ToolRegistry._adapter_failures.clear()
    yield
    ToolRegistry._adapter_failures.clear()


@pytest.fixture
def cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_CONFIG_CACHE", "1")


@pytest.fixture
def cache_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROBOTHOR_CONFIG_CACHE", raising=False)


def _adapter(name: str) -> AdapterConfig:
    return AdapterConfig(name=name, transport="stdio", command=["example-mcp-server"])


def _registry() -> Any:
    from robothor.engine.tools.registry import ToolRegistry

    with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
        return ToolRegistry()


async def _register(adapters: list[AdapterConfig]) -> Any:
    for a in adapters:
        mcp_client.register_adapter(a)
    registry = _registry()
    await registry.register_adapter_tools(adapters)
    return registry


# ── the flag ─────────────────────────────────────────────────────────────


class TestFlag:
    def test_default_off(self, cache_off: None) -> None:
        from robothor.engine.feature_flags import config_cache_enabled

        assert config_cache_enabled() is False

    def test_on(self, cache_on: None) -> None:
        from robothor.engine.feature_flags import config_cache_enabled

        assert config_cache_enabled() is True

    def test_declared_in_settings(self) -> None:
        from robothor.settings.model import FlagSettings

        envs = {
            f.json_schema_extra.get("env")
            for f in FlagSettings.model_fields.values()
            if isinstance(f.json_schema_extra, dict)
        }  # type: ignore[union-attr]
        assert "ROBOTHOR_CONFIG_CACHE" in envs


# ── tool-list cache ──────────────────────────────────────────────────────


class TestToolListCache:
    @pytest.mark.asyncio
    async def test_second_run_makes_zero_list_tools_calls(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        adapter = _adapter("alpha")

        first = await _register([adapter])
        second = await _register([adapter])

        assert counters.list_calls == {"alpha": 1}
        assert "alpha_tool" in first._schemas
        assert "alpha_tool" in second._schemas
        assert second.get_adapter_route("alpha_tool") == "alpha"

    @pytest.mark.asyncio
    async def test_session_drop_invalidates(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        adapter = _adapter("alpha")

        await _register([adapter])
        # The stdio server exits: the pool must start a new session, and a
        # list fetched from the dead one says nothing about the new one.
        pool._sessions["alpha"]._process.returncode = 1
        await _register([adapter])

        assert counters.list_calls == {"alpha": 2}
        assert counters.starts == {"alpha": 2}

    @pytest.mark.asyncio
    async def test_plugin_reload_invalidates(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        from robothor.plugins import loader

        counters = _install_fake_stdio(monkeypatch)
        adapter = _adapter("alpha")

        await _register([adapter])
        monkeypatch.setattr(loader, "_generation", loader._generation + 1)
        await _register([adapter])

        assert counters.list_calls == {"alpha": 2}

    @pytest.mark.asyncio
    async def test_changed_adapter_config_invalidates(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)

        await _register([_adapter("alpha")])
        changed = AdapterConfig(
            name="alpha", transport="stdio", command=["example-mcp-server", "--v2"]
        )
        await _register([changed])

        assert counters.list_calls == {"alpha": 2}

    @pytest.mark.asyncio
    async def test_explicit_refresh_refetches(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        mcp_client.register_adapter(_adapter("alpha"))

        await pool.list_tools_cached("alpha")
        await pool.list_tools_cached("alpha")
        assert counters.list_calls == {"alpha": 1}

        await pool.list_tools_cached("alpha", refresh=True)
        assert counters.list_calls == {"alpha": 2}

    @pytest.mark.asyncio
    async def test_mcp_list_tools_tool_honours_refresh(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        from robothor.engine.tools.handlers.mcp_client import HANDLERS

        counters = _install_fake_stdio(monkeypatch)
        mcp_client.register_adapter(_adapter("alpha"))
        handler = HANDLERS["mcp_list_tools"]

        r1 = await handler({"server_name": "alpha"}, None)
        r2 = await handler({"server_name": "alpha"}, None)
        assert counters.list_calls == {"alpha": 1}
        r3 = await handler({"server_name": "alpha", "refresh": True}, None)
        assert counters.list_calls == {"alpha": 2}
        assert r1["tools"] == r2["tools"] == r3["tools"]

    def test_mcp_list_tools_schema_declares_refresh(self) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas

        props = get_engine_schemas()["mcp_list_tools"]["function"]["parameters"]["properties"]
        assert props["refresh"]["type"] == "boolean"

    @pytest.mark.asyncio
    async def test_flag_off_lists_live_every_run(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_off: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        adapter = _adapter("alpha")

        await _register([adapter])
        await _register([adapter])

        assert counters.list_calls == {"alpha": 2}


# ── parallel connect ─────────────────────────────────────────────────────


class TestParallelConnect:
    @pytest.mark.asyncio
    async def test_three_slow_adapters_cost_one_latency(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        latency = 0.4
        names = ["alpha", "bravo", "charlie"]
        for n in names:
            counters.latency[n] = latency
        adapters = [_adapter(n) for n in names]

        t0 = time.perf_counter()
        registry = await _register(adapters)
        elapsed = time.perf_counter() - t0

        # Sequential is 3 x latency; concurrent is ~1 x.
        assert elapsed < latency * 1.75, f"took {elapsed:.2f}s"
        for n in names:
            assert registry.get_adapter_route(f"{n}_tool") == n

    @pytest.mark.asyncio
    async def test_hung_adapter_does_not_delay_the_others(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        monkeypatch.setattr(mcp_client, "ADAPTER_CONNECT_TIMEOUT_S", 0.3)
        counters.latency["hung"] = 30.0
        counters.latency["alpha"] = 0.05
        counters.latency["bravo"] = 0.05
        adapters = [_adapter("hung"), _adapter("alpha"), _adapter("bravo")]

        t0 = time.perf_counter()
        registry = await _register(adapters)
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.6, f"took {elapsed:.2f}s"
        assert registry.get_adapter_route("alpha_tool") == "alpha"
        assert registry.get_adapter_route("bravo_tool") == "bravo"
        assert registry.get_adapter_route("hung_tool") is None
        # The hung one still enters backoff, exactly like a sequential failure.
        assert "hung" in registry._adapter_failures

    @pytest.mark.asyncio
    async def test_duplicate_tool_name_resolves_in_adapter_order(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        """Concurrency must not make the winner of a name clash random."""
        counters = _install_fake_stdio(monkeypatch)
        real_list = mcp_client.McpClientSession.list_tools

        async def _same_name(self: Any) -> list[dict[str, Any]]:
            await real_list(self)
            # The first adapter answers last.
            await asyncio.sleep(0.1 if self.config.name == "alpha" else 0.0)
            return [{"name": "shared_tool", "description": self.config.name}]

        monkeypatch.setattr(mcp_client.McpClientSession, "list_tools", _same_name)
        registry = await _register([_adapter("alpha"), _adapter("bravo")])

        assert counters.list_calls == {"alpha": 1, "bravo": 1}
        assert registry.get_adapter_route("shared_tool") == "bravo"

    @pytest.mark.asyncio
    async def test_integrity_refusal_still_blocks_connection(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        pinned = AdapterConfig(
            name="pinned",
            transport="stdio",
            command=["/nonexistent/example-mcp-server"],
            command_sha256="0" * 64,
        )
        registry = await _register([pinned, _adapter("alpha")])

        assert "pinned" not in counters.starts
        assert registry.get_adapter_route("alpha_tool") == "alpha"


# ── shared HTTP client ───────────────────────────────────────────────────


class _HttpCounters:
    def __init__(self) -> None:
        self.constructed = 0
        self.closed = 0
        self.posts = 0


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch) -> _HttpCounters:
    counters = _HttpCounters()

    class _Resp:
        status_code = 200

        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body
            self.headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return self._body

        @property
        def text(self) -> str:
            return ""

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            counters.constructed += 1
            self.is_closed = False

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            await self.aclose()
            return False

        async def aclose(self) -> None:
            if not self.is_closed:
                counters.closed += 1
            self.is_closed = True

        async def post(self, url: str, headers: Any = None, json: Any = None) -> _Resp:
            counters.posts += 1
            if json.get("method") == "tools/list":
                return _Resp({"jsonrpc": "2.0", "id": json["id"], "result": {"tools": []}})
            return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "result": {}})

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return counters


def _http_session() -> mcp_client.McpHttpSession:
    return mcp_client.McpHttpSession(
        mcp_client.McpServerConfig(name="web", url="https://mcp.example.com/_mcp")
    )


class TestSharedHttpClient:
    @pytest.mark.asyncio
    async def test_client_reused_across_requests(
        self, monkeypatch: pytest.MonkeyPatch, cache_on: None
    ) -> None:
        counters = _install_fake_httpx(monkeypatch)
        session = _http_session()

        await session.list_tools()
        await session.list_tools()

        assert counters.posts >= 3  # initialize, initialized, tools/list x2
        assert counters.constructed == 1
        assert counters.closed == 0

        await session.stop()
        assert counters.closed == 1

    @pytest.mark.asyncio
    async def test_pool_shutdown_closes_client(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_httpx(monkeypatch)
        pool.register(mcp_client.McpServerConfig(name="web", url="https://mcp.example.com/_mcp"))
        session = await pool.get_session("web")
        await session.list_tools()

        await pool.shutdown()
        assert counters.constructed == 1
        assert counters.closed == 1

    @pytest.mark.asyncio
    async def test_flag_off_client_per_request(
        self, monkeypatch: pytest.MonkeyPatch, cache_off: None
    ) -> None:
        counters = _install_fake_httpx(monkeypatch)
        session = _http_session()

        await session.list_tools()

        assert counters.constructed == counters.posts


# ── pre-warm at daemon start ─────────────────────────────────────────────


class TestPrewarm:
    @pytest.mark.asyncio
    async def test_prewarm_then_first_run_lists_nothing(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        adapters = [_adapter("alpha"), _adapter("bravo")]

        warmed = await mcp_client.prewarm_adapter_sessions(adapters)
        assert warmed == {"alpha": 1, "bravo": 1}
        assert counters.list_calls == {"alpha": 1, "bravo": 1}

        await _register(adapters)
        assert counters.list_calls == {"alpha": 1, "bravo": 1}

    @pytest.mark.asyncio
    async def test_prewarm_skips_unverified_binary(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        pinned = AdapterConfig(
            name="pinned",
            transport="stdio",
            command=["/nonexistent/example-mcp-server"],
            command_sha256="0" * 64,
        )
        warmed = await mcp_client.prewarm_adapter_sessions([pinned])

        assert warmed == {}
        assert "pinned" not in counters.starts

    @pytest.mark.asyncio
    async def test_daemon_prewarm_is_flag_gated(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_off: None
    ) -> None:
        from robothor.engine import daemon

        counters = _install_fake_stdio(monkeypatch)
        monkeypatch.setattr(
            "robothor.engine.adapters.load_adapters", lambda *a, **k: [_adapter("alpha")]
        )

        await daemon._prewarm_mcp_adapters()
        assert counters.starts == {}

        monkeypatch.setenv("ROBOTHOR_CONFIG_CACHE", "1")
        await daemon._prewarm_mcp_adapters()
        assert counters.list_calls == {"alpha": 1}

    @pytest.mark.asyncio
    async def test_daemon_shutdown_stops_pool(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        from robothor.engine import daemon

        _install_fake_stdio(monkeypatch)
        mcp_client.register_adapter(_adapter("alpha"))
        await pool.get_session("alpha")

        await daemon._shutdown_mcp_pool()
        assert pool._sessions == {}


# ── review follow-ups: a failed fetch, a stale entry, a changed config ───


class TestAFailedFetchIsNeverCached:
    @pytest.mark.asyncio
    async def test_a_list_cancelled_mid_flight_does_not_poison_the_next_run(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        """A cancel mid-request leaves an unread response on a stdio pipe.

        Reusing that pipe reads the stale response as the next answer, which
        for ``tools/list`` parses as an empty list. The fake models exactly
        that: once a request on a session has been cancelled, every later
        ``list_tools`` on the SAME session answers ``[]``.
        """
        counters = _install_fake_stdio(monkeypatch)
        monkeypatch.setattr(mcp_client, "ADAPTER_CONNECT_TIMEOUT_S", 0.2)
        real_list = mcp_client.McpClientSession.list_tools
        first = {"done": False}

        async def _desyncing(self: Any) -> list[dict[str, Any]]:
            if getattr(self, "_poisoned", False):
                return []
            if not first["done"]:
                first["done"] = True
                self._poisoned = True
                # What the real session records once a request is on the pipe.
                self._desynced = True
                await asyncio.sleep(30)
            return await real_list(self)

        monkeypatch.setattr(mcp_client.McpClientSession, "list_tools", _desyncing)
        adapter = _adapter("slow")

        timed_out = await _register([adapter])
        assert timed_out.get_adapter_route("slow_tool") is None
        # The timeout put it in backoff, as before; the next run is a retry.
        type(timed_out)._adapter_failures.clear()

        retried = await _register([adapter])
        assert retried.get_adapter_route("slow_tool") == "slow"
        # The pipe the cancel left behind was thrown away, not reused.
        assert counters.starts == {"slow": 2}

    @pytest.mark.asyncio
    async def test_a_failed_list_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)
        real_list = mcp_client.McpClientSession.list_tools
        calls = {"n": 0}

        async def _fails_once(self: Any) -> list[dict[str, Any]]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("MCP error -32000: not ready")
            return await real_list(self)

        monkeypatch.setattr(mcp_client.McpClientSession, "list_tools", _fails_once)
        mcp_client.register_adapter(_adapter("alpha"))

        with pytest.raises(RuntimeError):
            await pool.list_tools_cached("alpha")
        tools = await pool.list_tools_cached("alpha")

        assert [t["name"] for t in tools] == ["alpha_tool"]
        assert counters.list_calls == {"alpha": 1}


class TestEntriesExpire:
    @pytest.mark.asyncio
    async def test_an_entry_older_than_the_ttl_is_refetched(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        """An HTTP session never drops, so age is the only thing that can retire its list."""
        counters = _install_fake_stdio(monkeypatch)
        monkeypatch.setattr(mcp_client, "TOOL_LIST_TTL_S", 0.05)
        mcp_client.register_adapter(_adapter("alpha"))

        await pool.list_tools_cached("alpha")
        await pool.list_tools_cached("alpha")
        assert counters.list_calls == {"alpha": 1}

        await asyncio.sleep(0.1)
        await pool.list_tools_cached("alpha")
        assert counters.list_calls == {"alpha": 2}

    def test_the_default_ttl_is_bounded(self) -> None:
        assert 0 < mcp_client.TOOL_LIST_TTL_S <= 3600


class TestAChangedConfigRestartsTheSession:
    @pytest.mark.asyncio
    async def test_the_refetch_comes_from_a_session_built_on_the_new_config(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)

        await _register([_adapter("alpha")])
        changed = AdapterConfig(
            name="alpha", transport="stdio", command=["example-mcp-server", "--v2"]
        )
        await _register([changed])

        assert counters.list_calls == {"alpha": 2}
        assert counters.starts == {"alpha": 2}
        assert pool._sessions["alpha"].config.command == ["example-mcp-server", "--v2"]

    @pytest.mark.asyncio
    async def test_flag_off_keeps_the_existing_session(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_off: None
    ) -> None:
        counters = _install_fake_stdio(monkeypatch)

        await _register([_adapter("alpha")])
        await _register(
            [AdapterConfig(name="alpha", transport="stdio", command=["example-mcp-server", "-x"])]
        )

        assert counters.starts == {"alpha": 1}


class TestShutdownOrdering:
    @pytest.mark.asyncio
    async def test_a_session_added_during_shutdown_does_not_break_it(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        _install_fake_stdio(monkeypatch)
        for n in ("alpha", "bravo"):
            mcp_client.register_adapter(_adapter(n))
        alpha = await pool.get_session("alpha")
        bravo = await pool.get_session("bravo")
        real_stop = type(alpha).stop
        stopped: list[str] = []

        async def _stop_and_race(self: Any) -> None:
            stopped.append(self.config.name)
            if self.config.name == "alpha":
                # A late caller sneaks a session in mid-shutdown.
                pool._sessions["late"] = type(self)(mcp_client.McpServerConfig(name="late"))
            await real_stop(self)

        monkeypatch.setattr(type(alpha), "stop", _stop_and_race)
        await pool.shutdown()

        assert {"alpha", "bravo"} <= set(stopped)
        assert bravo._process is None

    def test_the_daemon_stops_the_pool_after_everything_that_dispatches_tools(self) -> None:
        import inspect

        from robothor.engine import daemon

        src = inspect.getsource(daemon.main)
        pool_at = src.index("await _shutdown_mcp_pool()")
        for stopper in ("await scheduler.stop()", "await hooks.stop()", "await bot.stop("):
            assert src.index(stopper) < pool_at, stopper


# ── round 2: retire only a pipe that really is out of step ───────────────


class _PipeProcess:
    """A stdio MCP server at the pipe: Content-Length frames in, frames out.

    Each request is answered, with its own id, after ``delays[method]``
    seconds, exactly as a real server would answer it whether or not the
    client is still waiting.
    """

    def __init__(self, delays: dict[str, float]) -> None:
        self.returncode: int | None = None
        self.pid = 4343
        self.terminated = False
        self.stdout = asyncio.StreamReader()
        self._delays = delays
        self._buf = b""
        self._tasks: set[asyncio.Task[None]] = set()
        outer = self

        class _Stdin:
            def write(self, data: bytes) -> None:
                outer._feed(data)

            async def drain(self) -> None:
                return None

        self.stdin = _Stdin()

    def _feed(self, data: bytes) -> None:
        import json as _json

        self._buf += data
        while b"\r\n\r\n" in self._buf:
            head, rest = self._buf.split(b"\r\n\r\n", 1)
            length = int(head.split(b":")[1])
            if len(rest) < length:
                return
            body, self._buf = rest[:length], rest[length:]
            msg = _json.loads(body)
            if "id" not in msg:
                continue  # a notification
            task = asyncio.get_running_loop().create_task(self._answer(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _answer(self, msg: dict[str, Any]) -> None:
        import json as _json

        method = msg["method"]
        await asyncio.sleep(self._delays.get(method, 0.0))
        if method == "tools/list":
            result: dict[str, Any] = {"tools": [{"name": "pipe_tool", "description": "d"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": '{"ok": true}'}]}
        else:
            result = {}
        payload = _json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
        self.stdout.feed_data(b"Content-Length: %d\r\n\r\n" % len(payload) + payload)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return self.returncode or 0


def _install_pipe_stdio(
    monkeypatch: pytest.MonkeyPatch, delays: dict[str, float]
) -> list[_PipeProcess]:
    procs: list[_PipeProcess] = []

    async def _start(self: Any) -> None:
        proc = _PipeProcess(delays)
        procs.append(proc)
        self._process = proc

    monkeypatch.setattr(mcp_client.McpClientSession, "start", _start)
    return procs


class TestRetireOnlyADesyncedPipe:
    @pytest.mark.asyncio
    async def test_a_list_timing_out_behind_a_long_call_does_not_kill_it(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        procs = _install_pipe_stdio(monkeypatch, {"tools/call": 0.5})
        monkeypatch.setattr(mcp_client, "ADAPTER_CONNECT_TIMEOUT_S", 0.1)
        adapter = _adapter("pipe")
        mcp_client.register_adapter(adapter)
        session = await pool.get_session("pipe")

        call = asyncio.create_task(session.call_tool("pipe_tool", {}))
        await asyncio.sleep(0.05)  # the call now holds the session's request lock
        with pytest.raises(TimeoutError):
            await mcp_client.fetch_adapter_tools(adapter, pool)

        assert await call == {"content": [{"type": "text", "text": '{"ok": true}'}]}
        assert procs[0].terminated is False
        assert pool._sessions["pipe"] is session
        # Nothing was written for the list, so nothing is out of step: the
        # next miss lists on the same process.
        tools = await pool.list_tools_cached("pipe")
        assert [t["name"] for t in tools] == ["pipe_tool"]
        assert len(procs) == 1

    @pytest.mark.asyncio
    async def test_a_cancelled_call_does_not_poison_the_next_list(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        procs = _install_pipe_stdio(monkeypatch, {"tools/call": 0.1})
        mcp_client.register_adapter(_adapter("pipe"))
        session = await pool.get_session("pipe")

        call = asyncio.create_task(session.call_tool("pipe_tool", {}))
        await asyncio.sleep(0.03)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.15)  # its answer lands, unread, on the pipe

        tools = await pool.list_tools_cached("pipe")

        assert [t["name"] for t in tools] == ["pipe_tool"]
        assert len(procs) == 2
        assert procs[0].terminated is True

    @pytest.mark.asyncio
    async def test_a_cancelled_list_still_retires_its_pipe(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        procs = _install_pipe_stdio(monkeypatch, {"tools/list": 0.3})
        monkeypatch.setattr(mcp_client, "ADAPTER_CONNECT_TIMEOUT_S", 0.1)
        adapter = _adapter("pipe")
        mcp_client.register_adapter(adapter)

        with pytest.raises(TimeoutError):
            await mcp_client.fetch_adapter_tools(adapter, pool)
        await asyncio.sleep(0)

        assert procs[0].terminated is True
        assert "pipe" not in pool._sessions


class TestAFailedHttpListKeepsItsClient:
    @pytest.mark.asyncio
    async def test_the_shared_client_survives_and_nothing_is_cached(
        self, monkeypatch: pytest.MonkeyPatch, pool: Any, cache_on: None
    ) -> None:
        counters = _install_fake_httpx(monkeypatch)
        real_post = httpx.AsyncClient.post
        failures = {"left": 1}

        async def _post(self: Any, url: str, headers: Any = None, json: Any = None) -> Any:
            if json.get("method") == "tools/list" and failures["left"]:
                failures["left"] -= 1
                raise httpx.ReadTimeout("slow")
            return await real_post(self, url, headers=headers, json=json)

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        pool.register(mcp_client.McpServerConfig(name="web", url="https://mcp.example.com/_mcp"))
        session = await pool.get_session("web")

        with pytest.raises(httpx.ReadTimeout):
            await pool.list_tools_cached("web")
        await pool.list_tools_cached("web")

        assert pool._sessions["web"] is session
        assert counters.constructed == 1
        assert counters.closed == 0
