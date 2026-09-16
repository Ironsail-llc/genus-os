"""The `execute_code` handler, running a real subprocess.

`test_execute_code.py` probes the transport in isolation. This file probes what
the snippet can actually DO once it is running: what is in its environment,
what it can import, what happens when it overruns its time, its output budget
or its tool-call cap, and whether anything it starts survives the call.

These spawn a real interpreter, so they are a few seconds rather than
milliseconds. That is the price of testing a boundary instead of a mock of one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import textwrap
from pathlib import Path

import pytest

from robothor.engine.tool_proxy import clear_tool_proxy, set_tool_proxy
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.code_exec import _execute_code


class _StubProxy:
    """A proxy with no runner behind it, so the subprocess is the subject."""

    def __init__(self, *, allowed=("exec", "read_file"), max_calls=10, delay=0.0):
        self.allowed = frozenset(allowed)
        self.max_calls = max_calls
        self.calls_made = 0
        self.delay = delay
        self.seen: list[tuple[str, dict]] = []

    async def call(self, name, args):
        self.calls_made += 1
        self.seen.append((name, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"echo": name, "args": args}


@pytest.fixture
def workspace(tmp_path) -> Path:
    return tmp_path


def _timeout_setting_help() -> str:
    """The declared help for ROBOTHOR_EXECUTE_CODE_TIMEOUT, from the registry."""
    from robothor.settings.model import EngineSettings

    return str(EngineSettings.model_fields["execute_code_timeout"].description or "")


def _alive(pid: int) -> bool:
    """Is this pid still there? `signal 0` asks without sending anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reap(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(agent_id="probe-agent", run_id="", workspace=str(workspace))


async def _run(code: str, workspace: Path, *, proxy=None, timeout: int | None = None):
    proxy = proxy or _StubProxy()
    args: dict = {"code": textwrap.dedent(code)}
    if timeout is not None:
        args["timeout"] = timeout
    token = set_tool_proxy(proxy)
    try:
        return await _execute_code(args, _ctx(workspace)), proxy
    finally:
        clear_tool_proxy(token)


@pytest.mark.asyncio
class TestItRunsAtAll:
    async def test_stdout_comes_back(self, workspace):
        result, _ = await _run("print('hello from the snippet')", workspace)
        assert "hello from the snippet" in result["stdout"]
        assert result["returncode"] == 0

    async def test_a_traceback_comes_back_on_stderr_rather_than_as_a_crash(self, workspace):
        result, _ = await _run("raise ValueError('nope')", workspace)
        assert result["returncode"] != 0
        assert "ValueError" in result["stderr"]

    async def test_it_runs_from_the_workspace(self, workspace):
        (workspace / "marker.txt").write_text("found me")
        result, _ = await _run("print(open('marker.txt').read())", workspace)
        assert "found me" in result["stdout"]

    async def test_it_leaves_nothing_behind_but_what_the_snippet_wrote(self, workspace):
        await _run("print('x')", workspace)
        scratch = workspace / ".robothor" / "execute_code"
        leftovers = list(scratch.iterdir()) if scratch.is_dir() else []
        assert leftovers == [], f"per-call directory was not cleaned up: {leftovers}"


@pytest.mark.asyncio
class TestWhoMayCallIt:
    async def test_without_a_run_behind_it_there_is_no_allow_set_to_improvise(self, workspace):
        result = await _execute_code({"code": "print(1)"}, _ctx(workspace))
        assert "only available inside an agent run" in result["error"]

    async def test_an_agent_without_exec_is_refused(self, workspace):
        result, _ = await _run("print(1)", workspace, proxy=_StubProxy(allowed=("read_file",)))
        assert "needs the `exec` capability" in result["error"]

    async def test_an_empty_snippet_is_refused_rather_than_run(self, workspace):
        proxy = _StubProxy()
        token = set_tool_proxy(proxy)
        try:
            result = await _execute_code({"code": "   "}, _ctx(workspace))
        finally:
            clear_tool_proxy(token)
        assert "No code provided" in result["error"]


@pytest.mark.asyncio
class TestTheEnvironmentProbe:
    async def test_a_credential_in_the_engines_environment_is_not_in_the_childs(
        self, workspace, monkeypatch
    ):
        """The probe the brief names: read /proc/self/environ and see what is there."""
        monkeypatch.setenv("ROBOTHOR_PROBE_API_KEY", "sk-not-a-real-key-0123456789")
        result, _ = await _run(
            """
            import pathlib
            raw = pathlib.Path('/proc/self/environ').read_bytes().decode('utf-8', 'replace')
            print('PROBE_PRESENT' if 'ROBOTHOR_PROBE_API_KEY' in raw else 'PROBE_ABSENT')
            """,
            workspace,
        )
        assert "PROBE_ABSENT" in result["stdout"]
        assert "sk-not-a-real-key" not in result["stdout"]

    async def test_the_database_password_is_not_in_the_childs_environment(
        self, workspace, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "hunter2-not-real")
        result, _ = await _run(
            "import os; print(os.environ.get('ROBOTHOR_DB_PASSWORD', 'ABSENT'))", workspace
        )
        assert "ABSENT" in result["stdout"]

    async def test_the_engines_own_environ_is_not_readable_from_the_snippet(
        self, workspace, monkeypatch
    ):
        """The child's environment is scrubbed — and the snippet's PARENT is the
        engine, whose `/proc/<pid>/environ` a same-uid process may read.
        Measured on the first cut: one line returned all nine seeded
        credentials, while the report called this probe closed. The handler
        calls `harden_process()` (PR_SET_DUMPABLE=0) before spawning, so the
        guarantee holds for whatever process is running engine code rather than
        only for the daemon that remembered.

        This test hardens the pytest process, which is the point: a mocked
        `prctl` would prove nothing about the kernel.
        """
        monkeypatch.setenv("ROBOTHOR_PROBE_PARENT_TOKEN", "ghp_not-a-real-token-01234")
        result, _ = await _run(
            """
            import os
            try:
                raw = open(f'/proc/{os.getppid()}/environ', 'rb').read().decode('utf-8', 'replace')
                print('READ', 'HIT' if 'ghp_not-a-real-token' in raw else 'MISS')
            except PermissionError:
                print('BLOCKED')
            except FileNotFoundError:
                print('GONE')
            """,
            workspace,
        )
        assert "BLOCKED" in result["stdout"], result["stdout"]

    async def test_the_handler_hardens_before_it_spawns(self, workspace, monkeypatch):
        """Order, not just presence: hardening after the fork would leave the
        window this closes."""
        calls: list[str] = []
        import robothor.engine.process_hardening as hardening

        real = hardening.harden_process

        def spy():
            calls.append("hardened")
            return real()

        monkeypatch.setattr(hardening, "harden_process", spy)
        await _run("import os; print(os.getpid())", workspace)
        assert calls == ["hardened"]

    async def test_the_snippet_still_gets_a_working_process(self, workspace):
        """The scrub must not be so thorough that nothing runs."""
        result, _ = await _run(
            "import os; print('PATH' in os.environ, 'HOME' in os.environ)", workspace
        )
        assert "True True" in result["stdout"]


@pytest.mark.asyncio
class TestTheImportGuard:
    async def test_importing_the_engine_fails(self, workspace):
        result, _ = await _run(
            """
            try:
                import robothor
                print('IMPORTED')
            except ImportError as exc:
                print('REFUSED', exc)
            """,
            workspace,
        )
        assert "REFUSED" in result["stdout"]
        assert "IMPORTED" not in result["stdout"]

    async def test_importing_the_database_driver_fails(self, workspace):
        result, _ = await _run(
            """
            try:
                import psycopg2
                print('IMPORTED')
            except ImportError:
                print('REFUSED')
            """,
            workspace,
        )
        assert "REFUSED" in result["stdout"]

    async def test_an_ordinary_module_still_imports(self, workspace):
        result, _ = await _run("import json; print(json.dumps({'ok': True}))", workspace)
        assert '{"ok": true}' in result["stdout"]


@pytest.mark.asyncio
class TestTheToolProxyFromInsideTheSnippet:
    async def test_a_tool_call_round_trips(self, workspace):
        result, proxy = await _run(
            """
            from genus_tools import read_file
            print(read_file(path='notes.md'))
            """,
            workspace,
        )
        assert proxy.seen == [("read_file", {"path": "notes.md"})]
        assert "'echo': 'read_file'" in result["stdout"]
        assert result["tool_call_count"] == 1

    async def test_the_computed_form_works_too(self, workspace):
        _, proxy = await _run(
            """
            import genus_tools
            for name in ('read_file', 'read_file'):
                genus_tools.call(name, path=name)
            """,
            workspace,
        )
        assert len(proxy.seen) == 2

    async def test_a_tool_the_agent_cannot_reach_is_refused_from_code_too(self, workspace):
        result, proxy = await _run(
            """
            import genus_tools
            try:
                genus_tools.call('execute_code', code='print(1)')
                print('REACHED')
            except genus_tools.ToolError as exc:
                print('REFUSED', exc)
            """,
            workspace,
        )
        assert "REFUSED" in result["stdout"]
        assert proxy.seen == []

    async def test_the_call_cap_ends_the_loop_loudly(self, workspace):
        result, proxy = await _run(
            """
            import genus_tools
            made = 0
            try:
                for i in range(20):
                    genus_tools.call('read_file', path=str(i))
                    made += 1
            except genus_tools.ToolError:
                pass
            print('MADE', made)
            """,
            workspace,
            proxy=_StubProxy(max_calls=3),
        )
        assert "MADE 3" in result["stdout"]
        assert proxy.calls_made == 3
        assert result["tool_call_limit_reached"] is True

    async def test_the_socket_is_gone_once_the_call_returns(self, workspace):
        result, _ = await _run(
            """
            import os
            print('DIR', os.environ['GENUS_TOOLS_DIR'])
            """,
            workspace,
        )
        directory = result["stdout"].split("DIR ", 1)[1].strip()
        assert not Path(directory).exists()


@pytest.mark.asyncio
class TestTheBounds:
    async def test_a_snippet_that_sleeps_past_its_timeout_is_killed(self, workspace):
        result, _ = await _run(
            "import time; time.sleep(30); print('FINISHED')", workspace, timeout=1
        )
        assert result["timed_out"] is True
        assert "FINISHED" not in result["stdout"]
        assert "ran out of time" in result["error"]

    async def test_nothing_the_snippet_backgrounded_survives_the_call(self, workspace):
        marker = workspace / "survivor.txt"
        await _run(
            f"""
            import subprocess, sys
            subprocess.Popen([
                sys.executable, '-c',
                "import time; time.sleep(3); open({str(marker)!r}, 'w').write('alive')",
            ])
            print('SPAWNED')
            """,
            workspace,
        )
        await asyncio.sleep(4)
        assert not marker.exists(), "a backgrounded child outlived the snippet that started it"

    async def test_a_child_in_its_own_session_does_not_survive_either(self, workspace):
        """`start_new_session=True` takes a child OUT of the process group, so
        `killpg` alone never reached it. Measured on the first cut: 15 of 15
        survived."""
        marks = workspace / "sessions"
        marks.mkdir()
        await _run(
            f"""
            import subprocess
            for i in range(5):
                p = subprocess.Popen(['sleep', '120'], start_new_session=True)
                open({str(marks)!r} + f'/{{i}}', 'w').write(str(p.pid))
            print('SPAWNED')
            """,
            workspace,
        )
        await asyncio.sleep(0.5)
        pids = [int(f.read_text()) for f in marks.iterdir()]
        survivors = [pid for pid in pids if _alive(pid)]
        for pid in pids:
            _reap(pid)
        assert survivors == [], f"{len(survivors)} of {len(pids)} setsid children survived"

    async def test_a_double_forked_daemon_does_not_survive_either(self, workspace):
        """The classic daemonisation: fork, setsid, fork again, so the
        grandchild is orphaned and reparented away from the snippet."""
        marks = workspace / "daemons"
        marks.mkdir()
        await _run(
            f"""
            import os, time
            for i in range(3):
                if os.fork() == 0:
                    os.setsid()
                    if os.fork() == 0:
                        open({str(marks)!r} + f'/{{i}}', 'w').write(str(os.getpid()))
                        os.execvp('sleep', ['sleep', '120'])
                    os._exit(0)
            time.sleep(0.4)
            print('SPAWNED')
            """,
            workspace,
        )
        await asyncio.sleep(0.5)
        pids = [int(f.read_text()) for f in marks.iterdir()]
        survivors = [pid for pid in pids if _alive(pid)]
        for pid in pids:
            _reap(pid)
        assert survivors == [], f"{len(survivors)} of {len(pids)} daemons survived"

    async def test_a_detached_child_does_not_survive_a_timeout_either(self, workspace):
        """The snippet never reaches its own cleanup here, so this is the
        engine's census doing the work rather than the boot script's reaper."""
        marker = workspace / "timeout-child.pid"
        result, _ = await _run(
            f"""
            import subprocess, time
            p = subprocess.Popen(['sleep', '120'], start_new_session=True)
            open({str(marker)!r}, 'w').write(str(p.pid))
            time.sleep(60)
            """,
            workspace,
            timeout=2,
        )
        assert result["timed_out"] is True
        await asyncio.sleep(0.5)
        pid = int(marker.read_text())
        survived = _alive(pid)
        _reap(pid)
        assert not survived

    async def test_a_snippet_can_force_the_escape_deliberately(self, workspace):
        """The limit, pinned as a FACT rather than described as a risk.

        `start_new_session=True` puts the child outside the process group, and
        `os._exit` skips the `finally` in which the snippet would have reaped
        it — so the boot reaper never runs and the engine's census, which
        samples on a tick, has nothing to have seen. This is not a race a
        snippet might win; it is a thing a snippet can decide to do, and it
        works every time. The test exists so that nobody later writes a
        containment sentence this cannot back.
        """
        marker = workspace / "forced.pid"
        await _run(
            f"""
            import os, subprocess
            p = subprocess.Popen(['sleep', '120'], start_new_session=True)
            open({str(marker)!r}, 'w').write(str(p.pid))
            os._exit(0)
            """,
            workspace,
        )
        await asyncio.sleep(0.5)
        pid = int(marker.read_text())
        survived = _alive(pid)
        _reap(pid)
        assert survived, (
            "a snippet could no longer force an escape — if that is a real "
            "improvement, say so in docs/TOOLS.md, the timeout message and "
            "ROBOTHOR_EXECUTE_CODE_TIMEOUT's help before deleting this test"
        )

    async def test_the_result_says_what_was_and_was_not_killed(self, workspace):
        """The first cut promised "nothing it backgrounded survived" while 16
        of 16 did; the second said "may have survived", which reads as a race
        when it is something the snippet can choose. A sentence the control
        cannot back is worse than no sentence."""
        result, _ = await _run("import time; time.sleep(30)", workspace, timeout=1)
        message = result["error"]
        assert "process group and every descendant" in message
        assert "start_new_session=True" in message
        assert "os._exit" in message
        assert "nothing it backgrounded survived" not in message
        assert "may have survived" not in message

    async def test_every_place_that_describes_the_kill_says_the_same_thing(self):
        """Five texts, one claim. They drifted apart once already — the docs
        and the setting help promised containment the code never had — so the
        agreement is asserted rather than maintained by hand."""
        from robothor.engine import code_exec_process, code_exec_result
        from robothor.engine.sandbox_runtime import boot_template
        from robothor.engine.tools.handlers import code_exec

        texts = {
            "result": code_exec_result.shape.__doc__ or "",
            "timeout message": (code_exec_result.__doc__ or ""),
            "kill": code_exec_process.kill_descendants.__doc__ or "",
            "handler": code_exec.__doc__ or "",
            "boot reaper": boot_template.BOOT_TEMPLATE,
            "setting": _timeout_setting_help(),
            "docs": (Path(__file__).resolve().parents[3] / "docs" / "TOOLS.md").read_text(),
        }
        for name in ("kill", "handler", "setting", "docs", "boot reaper"):
            body = texts[name]
            assert "os._exit" in body, f"{name} does not name how the escape is forced"
        for name in ("setting", "docs"):
            assert "not a containment boundary" in texts[name], (
                f"{name} still reads as a containment promise"
            )

    async def test_oversized_stdout_is_cut_with_a_marker_and_spilled_to_a_file(
        self, workspace, monkeypatch
    ):
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.code_exec._settings_int",
            lambda name, default: 2_000 if name == "execute_code_max_output" else default,
        )
        result, _ = await _run("print('y' * 50_000)", workspace)
        assert result["stdout_truncated"] is True
        assert "[truncated" in result["stdout"]
        spilled = Path(result["stdout_file"])
        assert spilled.is_file()
        assert len(spilled.read_text()) > 40_000

    async def test_a_proxied_call_still_running_does_not_hold_the_tool_open(self, workspace):
        """`Server.wait_closed()` waits for every live handler, and a handler
        sits inside `await proxy.call(...)` until that tool returns. Measured on
        the first cut: a snippet with `timeout=2` and one proxied call sleeping
        12 s returned after 12.03 s — so a slow proxied tool, or one waiting on
        a person, held `execute_code` open long past the snippet's own deadline."""
        started = asyncio.get_running_loop().time()
        result, _ = await _run(
            """
            import genus_tools
            genus_tools.call('read_file', path='slow')
            print('NEVER')
            """,
            workspace,
            proxy=_StubProxy(delay=12.0),
            timeout=2,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert result["timed_out"] is True
        assert elapsed < 6, f"the handler held execute_code open for {elapsed:.1f}s"

    async def test_a_snippet_larger_than_the_source_cap_is_refused(self, workspace):
        proxy = _StubProxy()
        token = set_tool_proxy(proxy)
        try:
            result = await _execute_code({"code": "#" * 200_000}, _ctx(workspace))
        finally:
            clear_tool_proxy(token)
        assert "over the" in result["error"]

    async def test_output_past_the_hard_cap_is_never_held_whole(self, workspace, monkeypatch):
        """The spill is generous, not unbounded: a runaway loop printing far
        past it must not make the engine hold everything it printed first."""
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.code_exec._settings_int",
            lambda name, default: 1_000 if name == "execute_code_max_output" else default,
        )
        monkeypatch.setattr("robothor.engine.tools.handlers.code_exec.HARD_CAP_MULTIPLIER", 2)
        result, _ = await _run("print('z' * 500_000)", workspace)
        spilled = Path(result["stdout_file"])
        assert len(spilled.read_text()) < 100_000


@pytest.mark.asyncio
class TestCancellationIsNotAnEscapeHatch:
    """`registry.execute` wraps every handler in `asyncio.timeout`, and the run
    watchdog and the workflow deadline cancel the task outright. Measured on
    the first cut: a registry-style cancel at 2 s returned nothing and left the
    snippet AND its child running on the host with no deadline at all, socket
    directory deleted from under them. Every snippet longer than the agent's
    `tool_timeout_seconds` was an orphan — which is exactly the long research
    loop this tool exists for."""

    async def test_a_cancelled_handler_still_kills_the_snippet(self, workspace):
        marker = workspace / "cancelled.pid"
        code = f"""
            import os, time
            open({str(marker)!r}, 'w').write(str(os.getpid()))
            time.sleep(60)
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                await _run(code, workspace, timeout=30)
        await asyncio.sleep(0.5)
        pid = int(marker.read_text())
        assert not _alive(pid), "a cancelled execute_code left the snippet running"

    async def test_the_registry_deadline_is_wider_than_the_tools_own(self):
        """The two bounds must not compete. The tool's kill path fires first;
        the registry stays a backstop."""
        from robothor.engine.code_exec_process import DRAIN_GRACE_SECONDS
        from robothor.engine.runner import _resolve_tool_timeout
        from robothor.engine.tools.handlers.code_exec import MAX_TIMEOUT_SECONDS

        registry_bound = _resolve_tool_timeout("execute_code", 120)
        assert registry_bound > MAX_TIMEOUT_SECONDS + DRAIN_GRACE_SECONDS

    async def test_exec_carried_the_same_mismatch_and_no_longer_does(self):
        from robothor.engine.runner import _resolve_tool_timeout
        from robothor.engine.tools.handlers.filesystem import MAX_EXEC_TIMEOUT

        assert _resolve_tool_timeout("exec", 120) > MAX_EXEC_TIMEOUT

    async def test_an_ordinary_tool_keeps_the_agents_configured_cap(self):
        """The widening is per tool, not a general loosening."""
        from robothor.engine.runner import _resolve_tool_timeout

        assert _resolve_tool_timeout("read_file", 120) == 120


@pytest.mark.asyncio
class TestTheContainerCase:
    async def test_a_container_sandboxed_agent_is_refused_and_told_why(
        self, workspace, monkeypatch
    ):
        """Never a silent fall-back to the host: that would undo the isolation
        the manifest asked for while the result looked identical."""
        from robothor.engine.sandbox import Sandbox, SandboxMode

        sandbox = Sandbox(mode=SandboxMode.DOCKER, run_id="r", workspace=str(workspace))
        monkeypatch.setattr(
            "robothor.engine.sandbox.get_current_sandbox", lambda: sandbox, raising=False
        )
        result, _ = await _run("print(1)", workspace)
        assert "container-sandboxed agent" in result["error"]


def test_the_scratch_directory_is_not_a_secret_path():
    """`secret_paths` refuses anything under `.robothor/secret*`; the spill
    lives under `.robothor/` and must not collide with that prefix."""
    from robothor.engine.code_exec_result import WORKDIR_NAME

    assert WORKDIR_NAME.startswith(".robothor/")
    assert not WORKDIR_NAME.startswith(".robothor/secret")
    assert os.sep not in WORKDIR_NAME.removeprefix(".robothor/")
