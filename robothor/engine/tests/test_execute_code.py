"""`execute_code`: a snippet that can call the agent's tools, and cannot do more.

This is the largest new attack surface the engine has grown, so the tests are
written as probes rather than as feature coverage. Each one names the thing an
attacker would try:

* read the engine's credentials out of the child environment;
* import the engine and reach the database through it;
* call a tool the agent is not allowed to call;
* forge a ``tool_call_id`` so a proxied call looks like something else;
* leave a background process behind when the snippet returns;
* spend an unbounded number of tool calls, seconds, or bytes.

The ones that are bounded rather than closed say so in the test name, because
a probe whose answer is "bounded" and a probe whose answer is "impossible" are
different claims and this file is where the difference is recorded.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
from pathlib import Path

import pytest

from robothor.engine.code_exec_rpc import ToolRpcServer
from robothor.engine.code_execution import MAX_STDERR_BYTES, truncate_with_marker
from robothor.engine.tool_proxy import PROXY_DENIED_TOOLS, ToolProxy
from tests.platform_capabilities import needs_peer_credentials

CLIENT_SOURCE = Path(__file__).resolve().parents[1] / "sandbox_runtime" / "genus_tools.py"


class _FakeProxy(ToolProxy):
    """A proxy that records what reached it, without a runner behind it."""

    def __init__(self, *, allowed=("read_file", "web_fetch"), max_calls=5):
        self.seen: list[tuple[str, dict]] = []
        self._allowed = frozenset(allowed)
        self.max_calls = max_calls
        self.calls_made = 0

    async def call(self, name: str, args: dict) -> dict:
        self.calls_made += 1
        self.seen.append((name, args))
        if name not in self._allowed:
            return {"error": f"Tool '{name}' is not available to this agent."}
        return {"ok": True, "tool": name, "args": args}


async def _serve(proxy, tmp_path, *, session: int | None = None):
    """A live server bound to THIS process's session.

    The requests below come from this process, so binding to our own session is
    what the spawn does for a real snippet — see `bind_to_session`. Pass
    `session=` to stand in for another run.
    """
    server = ToolRpcServer(
        directory=tmp_path,
        proxy=proxy,
        max_calls=proxy.max_calls,
    )
    await server.start()
    server.bind_to_session(os.getsid(0) if session is None else session)
    return server


#: What `_request` returns when the connection ENDED rather than the server
#: answering. Its own marker, so "refused" and "never spoke" stay two
#: different facts: three tests assert only `ok is False`, and mapping a dead
#: socket onto that would let a server that simply never replies satisfy all
#: of them.
_TRANSPORT_CLOSED = {"ok": False, "transport": "closed"}


def _request(server, payload: dict) -> dict:
    """One raw request over the socket, as the sandboxed client makes it.

    A refused connection reaches the client as a broken pipe or a reset just
    as often as it does as an empty reply — the server can close before this
    thread has finished writing — so all three of those shapes return
    `_TRANSPORT_CLOSED`, deterministically, and a test that needs the SERVER
    to have spoken checks for its absence.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(server.socket_path))
            sock.sendall(json.dumps(payload).encode() + b"\n")
            chunks = b""
            while not chunks.endswith(b"\n"):
                piece = sock.recv(65536)
                if not piece:
                    break
                chunks += piece
    except (BrokenPipeError, ConnectionResetError):
        return dict(_TRANSPORT_CLOSED)
    if not chunks.strip():
        return dict(_TRANSPORT_CLOSED)
    return json.loads(chunks.decode())


def _spoke(reply: dict) -> bool:
    """Did the SERVER answer, rather than the connection dying?"""
    return reply.get("transport") != "closed"


class TestTheClientModuleIsShippable:
    """The file copied into the sandbox has to run with nothing installed."""

    def test_it_imports_only_the_standard_library(self):
        tree = ast.parse(CLIENT_SOURCE.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert imported <= {"json", "os", "pathlib", "socket", "typing", "__future__"}

    def test_it_names_no_engine_module(self):
        assert "robothor" not in CLIENT_SOURCE.read_text()


#: The smallest ``sun_path`` among the kernels this runs on: 104 bytes on
#: macOS and the BSDs (108 on Linux), including the terminating NUL.
_SUN_PATH_MAX = 104


@pytest.fixture
def socket_dir():
    """A per-test directory short enough to bind an AF_UNIX socket in.

    Under the system temp directory, as production's per-run scratch root is
    (`code_exec._run_scratch_root`), not under pytest's much longer tmp_path.
    """
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="rpc-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class TestTheTransport:
    def test_the_socket_these_tests_bind_fits_every_kernel(self, socket_dir):
        """The transport tests bind a real AF_UNIX socket, and the kernel caps
        its path. pytest's own tmp_path is ~150 bytes on macOS, so every test
        below failed there with "AF_UNIX path too long" before it tested
        anything; a short directory is what production uses too
        (`code_exec._run_scratch_root`)."""
        server = ToolRpcServer(directory=socket_dir, proxy=_FakeProxy(), max_calls=5)
        assert len(os.fsencode(server.socket_path)) < _SUN_PATH_MAX, server.socket_path

    @pytest.mark.asyncio
    async def test_a_kernel_without_peer_credentials_refuses_rather_than_hangs(
        self, socket_dir, monkeypatch
    ):
        """``SO_PEERCRED`` is Linux's. Where the kernel has no such option the
        peer cannot be identified, and that has to be the same answer as any
        other unidentifiable peer: a refusal, sent. It used to raise inside the
        handler before replying, so the connection stayed open and the snippet
        blocked in ``recv`` until something killed it — the engine suite's
        hang on macOS."""
        monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)  # a hang fails this test instead of the suite
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": server.token, "op": "call", "name": "read_file", "args": {}},
            )
        finally:
            socket.setdefaulttimeout(previous)
            await server.aclose()
        assert _spoke(reply), "the server must REFUSE an unidentifiable peer, not hang up"
        assert reply.get("ok") is False
        assert proxy.seen == []

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_a_call_reaches_the_proxy_and_the_result_comes_back(self, socket_dir):
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            token = server.token
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": token, "op": "call", "name": "read_file", "args": {"path": "a"}},
            )
        finally:
            await server.aclose()
        assert reply["ok"] is True
        assert reply["result"]["tool"] == "read_file"
        assert proxy.seen == [("read_file", {"path": "a"})]

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_a_wrong_token_never_reaches_the_proxy(self, socket_dir):
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": "not-the-token", "op": "call", "name": "read_file", "args": {}},
            )
        finally:
            await server.aclose()
        assert _spoke(reply), "the server must REFUSE a bad token, not drop the connection"
        assert reply.get("ok") is False
        assert proxy.seen == []

    @pytest.mark.asyncio
    async def test_the_socket_directory_is_private_to_this_user(self, socket_dir):
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            mode = server.directory.stat().st_mode & 0o777
            token_mode = server.token_path.stat().st_mode & 0o777
        finally:
            await server.aclose()
        assert mode == 0o700
        assert token_mode == 0o600

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_the_call_cap_refuses_the_rest_of_the_snippet(self, socket_dir):
        proxy = _FakeProxy(max_calls=2)
        server = await _serve(proxy, socket_dir)
        try:
            call = {"token": server.token, "op": "call", "name": "read_file", "args": {}}
            replies = [await asyncio.to_thread(_request, server, call) for _ in range(4)]
        finally:
            await server.aclose()
        assert [r["ok"] for r in replies] == [True, True, False, False]
        assert replies[2]["code"] == "call_cap"
        assert proxy.calls_made == 2, "the cap let a call through to the proxy"

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_a_forged_tool_call_id_is_ignored(self, socket_dir):
        """The wire has no id field. A snippet cannot name one, and the proxy
        mints its own — so there is nothing to forge."""
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {
                    "token": server.token,
                    "op": "call",
                    "name": "read_file",
                    "args": {},
                    "tool_call_id": "call_pretend_to_be_something_else",
                },
            )
        finally:
            await server.aclose()
        assert reply["ok"] is True
        assert proxy.seen == [("read_file", {})]

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_a_denied_tool_never_reaches_the_proxy(self, socket_dir):
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": server.token, "op": "call", "name": "execute_code", "args": {}},
            )
        finally:
            await server.aclose()
        assert reply["ok"] is False
        assert reply["code"] == "denied"
        assert proxy.seen == []

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_an_oversized_request_is_refused_rather_than_buffered(self, socket_dir):
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            huge = {
                "token": server.token,
                "op": "call",
                "name": "read_file",
                "args": {"x": "a" * (server.max_request_bytes + 1024)},
            }
            reply = await asyncio.to_thread(_request, server, huge)
        finally:
            await server.aclose()
        assert _spoke(reply), "the server must ANSWER and hang up, not just hang up"
        assert reply.get("ok") is False
        assert reply.get("code") == "too_large"
        assert proxy.seen == []


class TestOnlyThisSnippetMaySpeak:
    """The token is not the control. Every snippet on the box runs as the
    engine uid, so 0700 and 0600 exclude nobody that matters: a snippet under
    run A could read run B's token out of the temp directory and drive B's
    proxy with B's agent config, B's allow-set, B's RBAC identity and B's step
    trail. Probed before the fix — it did. So the socket asks the kernel who is
    speaking."""

    @pytest.mark.asyncio
    async def test_a_peer_in_another_session_is_refused_with_its_token(self, socket_dir):
        proxy = _FakeProxy()
        # Bound to a session this process is NOT in. pid 1 is init: always
        # alive, always its own session, never ours.
        server = await _serve(proxy, socket_dir, session=1)
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": server.token, "op": "call", "name": "read_file", "args": {}},
            )
        finally:
            await server.aclose()
        # The security invariant is the last line and is asserted whatever
        # happens on the wire. The refusal CODE is only checkable when the
        # server got its answer out before hanging up — it closes the
        # connection immediately after an auth refusal, so which of the two
        # the client sees is a race the client does not control.
        assert reply["ok"] is False
        if _spoke(reply):
            assert reply["code"] == "auth"
        assert proxy.seen == [], "another run's snippet reached this proxy"

    @pytest.mark.asyncio
    async def test_an_unbound_socket_serves_nobody(self, socket_dir):
        """Between `start()` and the spawn there is no legitimate caller, so
        there is no legitimate answer either."""
        proxy = _FakeProxy()
        server = ToolRpcServer(directory=socket_dir, proxy=proxy, max_calls=5)
        await server.start()
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": server.token, "op": "call", "name": "read_file", "args": {}},
            )
        finally:
            await server.aclose()
        # The only test that accepts a dead connection as the answer: an
        # unbound socket may refuse in either shape, and which one the client
        # sees is a race it does not control.
        assert reply["ok"] is False
        assert proxy.seen == []

    @needs_peer_credentials
    @pytest.mark.asyncio
    async def test_the_right_session_is_still_served(self, socket_dir):
        """The check must not be so eager that it refuses the snippet — the
        failure mode that makes a control look like it works."""
        proxy = _FakeProxy()
        server = await _serve(proxy, socket_dir)
        try:
            reply = await asyncio.to_thread(
                _request,
                server,
                {"token": server.token, "op": "call", "name": "read_file", "args": {}},
            )
        finally:
            await server.aclose()
        assert reply["ok"] is True


class TestWhatTheProxyRefusesOutright:
    def test_recursion_is_impossible(self):
        assert "execute_code" in PROXY_DENIED_TOOLS

    def test_a_snippet_cannot_start_a_run(self):
        assert {"spawn_agent", "spawn_agents"} <= PROXY_DENIED_TOOLS

    def test_a_snippet_cannot_page_a_person(self):
        assert "ask_user" in PROXY_DENIED_TOOLS


class TestOutputBounding:
    def test_stdout_past_the_cap_says_how_much_was_cut(self):
        text, cut = truncate_with_marker("x" * 200, 50)
        assert cut is True
        assert "[truncated" in text

    def test_a_short_stream_is_untouched(self):
        text, cut = truncate_with_marker("hello", MAX_STDERR_BYTES)
        assert (text, cut) == ("hello", False)
