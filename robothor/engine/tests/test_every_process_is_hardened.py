"""Every long-running Genus process, not only the engine.

Review R5. Round 1 called ``harden_process()`` from ``daemon.main`` and nowhere
else, so on this box the bridge (which holds the SSO secret and the database
credentials), the orchestrator, the vision service, the four connector bridges
and the two MCP servers all stayed dumpable — and an ``exec`` child is the same
uid as every one of them. Hardening one process out of ten is not a boundary,
it is a statistic.

Also R5's second half: the denylist matched ``/proc/<pid>/environ`` literally,
so ``/proc/<pid>//environ`` walked straight past it. A path is normalised
before it is matched now, which is the only way a path denylist can mean
anything.

The entry-point test imports each module and asserts the call is THERE, rather
than starting ten services. A grep would pass on a commented-out line; an
import-and-inspect will not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from robothor.engine.secret_paths import exec_reads_secret

REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``(label, the code that starts this service the way the box starts it)``.
#:
#: RUN, not parsed. The round-2 test parsed each file and passed on
#: ``robothor/api/mcp.py`` because the call sat in its ``__main__`` block — and
#: the box starts that server through ``robothor mcp`` → ``cli.admin.cmd_mcp``,
#: which never touches that block. Both live MCP processes were dumpable while
#: a green test said otherwise. A parse proves a call exists in a file; only
#: running the entry point proves the startup path reaches it.
#:
#: Each snippet patches the service body to a no-op, calls the real entry
#: function, and prints the owner of ``/proc/self/environ`` — uid 0 once the
#: process is non-dumpable, the caller's uid while it is readable.
ENTRY_POINTS = [
    (
        "engine daemon",
        "from robothor.engine import daemon; daemon._harden_and_state_posture()",
    ),
    (
        "mcp server (robothor mcp -> cmd_mcp -> run_server)",
        "import asyncio, robothor.api.mcp as m;"
        "m.create_server = lambda: None;"
        "m.stdio_server = None;"
        "import robothor.engine.process_hardening as ph;"
        "asyncio.run(_only_the_hardening(m))",
    ),
    (
        "orchestrator (uvicorn imports the module)",
        "import robothor.api.orchestrator  # noqa: F401",
    ),
    (
        "connector bridge",
        "import robothor.connectors.rest_mcp_bridge as b;"
        "b.asyncio = type('x', (), {'run': staticmethod(lambda *a, **k: None)});"
        "b.main()",
    ),
    (
        "vision service",
        "import robothor.vision.service as v;"
        "v.logging = type('x', (), {'basicConfig': staticmethod(lambda **k: None),"
        " 'getLogger': staticmethod(lambda *a: None), 'INFO': 20})();"
        "v.VisionService = _stop_before_it_starts;"
        "_call_until_it_stops(v.main)",
    ),
]

#: Helpers injected into each subprocess. ``run_server`` is an async function
#: whose body needs a real stdio transport, so we call only the part under test
#: — and a service ``main()`` that goes on to serve is stopped once it has
#: hardened, which is the only thing being asserted.
#:
#: ``_stop_before_it_starts`` is how that stop is made unconditional. The vision
#: entry went on to ``asyncio.start_server`` on the health port and then into a
#: detection loop that never returns. On this box the live vision service
#: already holds that port, so the bind raised, the probe fell through and the
#: case passed in half a second; on a runner the port is free, the server starts
#: and pytest-timeout kills the test at 30s — which is exactly how it failed on
#: every Python version in CI. A probe that depends on a port being taken in
#: order to terminate is not a probe, and nothing here needs a real server to
#: prove a call happened before one.
#:
#: So the boundary object is replaced and the state is read at that point: the
#: entry has hardened, and nothing has bound, connected or loaded a model. That
#: makes the ORDER the assertion rather than a hope — ``_environ_owner`` fails
#: on the worst observation, so an entry that hardened only after starting its
#: service would still be caught.
_PRELUDE = """
import asyncio, os, sys


class _Stop(Exception):
    pass


async def _only_the_hardening(module):
    from robothor.engine.process_hardening import harden_process
    import inspect
    source = inspect.getsource(module.run_server)
    assert "harden_process" in source, "run_server does not harden"
    harden_process()


def _call_until_it_stops(fn):
    try:
        fn()
    except BaseException:
        pass


def _stop_before_it_starts(*_args, **_kwargs):
    # Stands in for whatever an entry constructs AFTER it has hardened, and
    # reads the state HERE -- see the note above `_PRELUDE`.
    print(os.stat('/proc/self/environ').st_uid)
    raise _Stop()
"""


def _environ_owner(snippet: str) -> int:
    """Run *snippet* in a subprocess and return the uid owning its own
    ``/proc/self/environ``. Root (0) means the process is non-dumpable.

    The WORST observation, not the last: a snippet may look more than once --
    the vision entry reads the state at the moment it would have constructed
    the service, as well as at the end -- and a process that hardened late
    would show the caller's uid at the earlier look. Taking the maximum makes
    every look count.
    """
    script = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        + _PRELUDE
        + "\ntry:\n"
        # Stripped: a `; ` separator leaves a leading space that becomes a
        # deeper indent than the `try:` block and an IndentationError.
        + "".join(f"    {line.strip()}\n" for line in snippet.split(";") if line.strip())
        + "except _Stop:\n    pass\n"
        + "except BaseException as exc:\n    print('ERR', type(exc).__name__, file=sys.stderr)\n"
        + "print(os.stat('/proc/self/environ').st_uid)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    seen = [int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()]
    assert seen, f"no uid printed; stderr={proc.stderr[-400:]}"
    return max(seen)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is a Linux interface")
@pytest.mark.parametrize(("label", "snippet"), ENTRY_POINTS, ids=[e[0] for e in ENTRY_POINTS])
def test_every_entry_point_hardens_the_process_it_starts(label, snippet):
    owner = _environ_owner(snippet)
    assert owner == 0, (
        f"{label}: /proc/self/environ is still owned by uid {owner}, so an agent's "
        "exec child — same uid — can read this process's environment"
    )


def test_the_helper_is_importable_from_one_place():
    """One helper, so a new entry point copies a single line and cannot get a
    subtly different version of it."""
    from robothor.engine.process_hardening import harden_process

    assert callable(harden_process)


# ── the // gap ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/self/environ",
        "cat /proc/self//environ",
        "cat /proc//self/environ",
        "cat /proc/self/./environ",
        "cat /proc/1234/task/1234/environ",
        "cat /proc/self/task/99/environ",
        "grep -a FAKE /proc/$PPID//environ",
        "tr '\\0' '\\n' < /proc/self//environ",
        "cat /proc/self//cmdline",
        "cat //proc/self/environ",
    ],
)
def test_every_spelling_of_the_procfs_read_is_refused(command):
    """``/proc/<pid>//environ`` slipped the literal match. A path denylist that
    matches the string rather than the path is a denylist that means nothing —
    the kernel resolves all of these to the same file."""
    assert exec_reads_secret(command) is not None, f"{command!r} was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/loadavg",
        "cat /proc/self/status",
        "ls /proc/self",
        "cat /proc//loadavg",
        "echo /environ",
        "cat environ.txt",
    ],
)
def test_ordinary_proc_reads_still_work(command):
    """A denylist that eats `/proc/self/status` breaks every health script, and
    a control that breaks scripts gets turned off."""
    assert exec_reads_secret(command) is None, f"{command!r} was refused"


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/123/../123/environ",
        "cat /proc/self/../self/environ",
        "cat /proc/1/task/1/../../environ",
        "grep -a X /proc/$PPID/../$PPID/environ",
        "cat /proc/$(pgrep engine)/environ",
        "cd /proc/1234 && cat environ",
        "P=/proc/1/environ; cat $P",
    ],
)
def test_a_computed_or_relative_procfs_path_is_refused_too(command):
    """Review N4's second half, and the end of the spelling game.

    ``/proc/<pid>/../<pid>/environ`` read a canary while every other spelling
    was refused; a regex written for that still missed
    ``/proc/1/task/1/../../environ``; and neither could ever have caught a path
    the SHELL computes. Paths are normalised with ``posixpath`` now, and a
    blunt backstop refuses any command that mentions ``/proc`` and asks for
    ``environ`` — whatever lies between them.
    """
    assert exec_reads_secret(command) is not None, f"{command!r} was allowed"


# ── N4: the engine's own long-lived children ─────────────────────────────────


@pytest.mark.asyncio
async def test_an_mcp_stdio_server_does_not_inherit_the_engine_environment(monkeypatch):
    """An MCP stdio server lives as long as the engine and held its whole
    environment — ~50 credentials — readable through procfs, because
    ``PR_SET_DUMPABLE`` does not survive ``execve``.

    Its own ``config.env`` is exactly where an MCP server's credentials belong;
    nothing else in the engine's environment is its business.
    """
    import robothor.engine.mcp_client as mcp_client

    canary = "ghp_FAKE0000CANARYaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setenv("GENUS_SEC1_MCP_CANARY", canary)
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")

    seen: dict[str, dict[str, str]] = {}

    async def fake_exec(*_argv, env=None, **_kw):
        seen["env"] = dict(env or {})

        class _Proc:
            pid = 1234
            stdin = stdout = stderr = None

        return _Proc()

    monkeypatch.setattr(mcp_client.asyncio, "create_subprocess_exec", fake_exec)

    config = type(
        "Cfg",
        (),
        {"name": "probe", "command": ["/bin/true"], "env": {"MCP_OWN_KEY": "fake-0000"}},
    )()
    await mcp_client.McpClientSession(config).start()

    handed = seen.get("env", {})
    leaked = canary in "".join(handed.values())
    assert not leaked, "an MCP stdio server was handed the engine's credentials"
    assert handed.get("MCP_OWN_KEY") == "fake-0000", (
        "the server's own declared env must still reach it — that is what it is for"
    )


# ── N5: the vault master key ─────────────────────────────────────────────────


def test_the_vault_master_key_is_a_secret_path():
    """It decrypts every row the vault holds, and ``ROBOTHOR_WORKSPACE`` — which
    says where it lives — is on the exec allowlist. The ``*.key`` pattern missed
    it because of the hyphen."""
    from robothor.engine.secret_paths import is_secret_path

    assert is_secret_path("/workspace/.vault-key")
    assert is_secret_path("~/robothor/.vault-key")


@pytest.mark.parametrize(
    "command",
    [
        "cat $ROBOTHOR_WORKSPACE/.vault-key",
        "base64 /ws/.vault-key",
        "xxd /ws/.vault-key",
    ],
)
def test_an_exec_child_cannot_print_the_vault_master_key(command):
    assert exec_reads_secret(command) is not None, f"{command!r} was allowed"


@pytest.mark.asyncio
async def test_a_real_exec_child_is_refused_the_key(tmp_path, monkeypatch):
    """Through the handler, with a real workspace, because the refusal has to be
    on the path an agent actually takes."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.filesystem import HANDLERS

    (tmp_path / ".vault-key").write_bytes(b"0" * 32)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")

    result = await HANDLERS["exec"](
        {"command": "cat $ROBOTHOR_WORKSPACE/.vault-key", "timeout": 10},
        ToolContext(agent_id="worker", workspace=str(tmp_path)),
    )
    assert "error" in result
    assert "stdout" not in result
