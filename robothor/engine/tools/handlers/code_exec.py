"""`execute_code` — a Python snippet that can call the agent's tools.

MEASURED 2026-09-16. On the three WildClawBench Productivity tasks this engine
scored 0.000 on, the competing harness imported its tool library inside a code
sandbox 52, 13 and 7 times: a hundred and thirty papers classified in one model
turn instead of a hundred and thirty. Our `web_fetch` is a turn-level tool, so
the same loop costs a hundred and thirty turns, which no context window holds.
The gap there is not research ability; it is that an O(N) research loop cost us
O(N) turns and cost them one.

What runs, and where
--------------------
A subprocess, from the workspace, with:

* **the scrubbed child environment, at `enforce`, always.** `exec` ships the
  ladder (`ROBOTHOR_EXEC_ENV_MODE`) because it has years of agents behind it
  that an upgrade must not break. This tool has none, so it ships closed: the
  child gets the process essentials, the declared non-secret `ROBOTHOR_*`
  settings, and this agent's own `secrets:` grants. No database password, no
  provider key, no channel token — which is also why `/proc/self/environ` is a
  probe with a boring answer here.
* **an isolated interpreter** (`-I`: no `PYTHONPATH`, no user site, no implicit
  script directory) plus an import guard that refuses the engine's own
  packages. The guard is a guardrail and NOT a boundary — a determined snippet
  can remove a meta-path finder — and it does not need to be one: the boundary
  is that the environment holds nothing worth importing the engine for.
* **its own process group**, killed on the way out whether the snippet finished
  or timed out, so nothing it backgrounded outlives the call.

What it can reach
-----------------
`genus_tools`, over a unix socket in a 0700 directory, and nothing else. Every
proxied call goes back through the engine's own admission — plan mode,
`tools_allowed`, the PRE_TOOL_USE hook, the guardrail engine, RBAC — and then
through `registry.execute`, where the repeat guard, the benchmark sandbox and
the audit row already live. See `robothor/engine/tool_proxy.py`.

Why it requires `exec`
----------------------
An agent granted `exec` can already run `python3 - <<EOF` with the same
filesystem and the same process capabilities. Gating `execute_code` on `exec`
therefore means this tool adds exactly one capability over what its callers
already had — the tool proxy — and that one is bounded by admission. An agent
that cannot run commands does not get a way to run commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.code_execution import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    SandboxResult,
    truncate_with_marker,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


#: The most source one call may carry. A snippet is code; a caller sending a
#: megabyte of it is sending data, and data belongs in a file the code reads.
MAX_CODE_CHARS = 100_000

#: How much more than the model's stdout budget is kept for the spill file.
#: The spill exists so the agent can page the whole output back, so it has to
#: hold more than the inline cut — and it cannot hold everything, because
#: "everything" is whatever a runaway loop printed.
HARD_CAP_MULTIPLIER = 20

#: The ceiling an agent cannot ask past, matching `exec`'s. A snippet that
#: outlives the run owning it is a leak, not a long job.
MAX_TIMEOUT_SECONDS = 900

#: How often the exit poll wakes. Short enough that a snippet printing one line
#: does not feel slow, long enough that a fifteen-minute one costs a few
#: thousand cheap wakeups rather than a hundred thousand.
EXIT_POLL_SECONDS = 0.05

#: After the process group is killed, how long the pipes get to give up what
#: they still hold. Bounded because an orphan the kill could not reach (one
#: that changed its own group) would otherwise hold this call open.
DRAIN_GRACE_SECONDS = 5.0

#: Where an oversized stdout is spilled. Under the workspace so the agent can
#: `read_file` it back; under `.robothor/` so it is never mistaken for a
#: deliverable; not under `.robothor/secret*`, which is the prefix
#: `secret_paths` refuses.
WORKDIR_NAME = ".robothor/execute_code"

#: Top-level packages the in-sandbox import guard refuses. Defence in depth
#: only — see the module docstring — but it turns "the snippet imported the
#: engine" from something nobody would notice into an ImportError the model
#: reads and works around.
GUARDED_IMPORTS: tuple[str, ...] = (
    "robothor",
    "crm",
    "psycopg2",
    "litellm",
    "redis",
)

_BOOT_TEMPLATE = '''\
"""Written by the engine. Runs the agent's snippet with genus_tools importable."""

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

_GUARDED = {guarded!r}


class _RefuseEngineImports:
    """The engine's own packages are not part of the sandbox's vocabulary."""

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy hook
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _GUARDED:
            raise ImportError(
                fullname
                + " is not importable from execute_code. Call tools through "
                "genus_tools instead."
            )
        return None


sys.meta_path.insert(0, _RefuseEngineImports())

import runpy  # noqa: E402

runpy.run_path(os.path.join(_DIR, "snippet.py"), run_name="__main__")
'''


def resolve_timeout(args: dict[str, Any], default: int) -> int:
    """What the snippet gets: what it asked for, inside the ceiling.

    Tolerant of what models emit — `"120"` as often as `120` — and falls back
    rather than raising, because a malformed timeout must not turn a working
    snippet into a tool error.
    """
    raw = args.get("timeout", default)
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return default
    if requested <= 0:
        return default
    return min(requested, MAX_TIMEOUT_SECONDS)


def _settings_int(path: str, default: int) -> int:
    try:
        from robothor.settings import get_settings

        return int(getattr(get_settings().engine, path))
    except Exception:  # noqa: BLE001 - a missing config is the default, not a crash
        return default


def _child_environment(ctx: ToolContext, tools_dir: Path) -> dict[str, str]:
    """The environment the snippet actually gets.

    Built at `enforce` regardless of the instance's `ROBOTHOR_EXEC_ENV_MODE`
    rung: this tool has no installed base to protect, so the closed setting is
    free here and the open one would be a new way to hand a snippet the ~50
    credentials the box booted with.
    """
    from robothor.engine.exec_env import MODE_ENFORCE, build_exec_env, grants_for_agent

    agent_id = getattr(ctx, "agent_id", "") or ""
    workspace = getattr(ctx, "workspace", "") or ""
    built = build_exec_env(
        agent_id=agent_id,
        mode=MODE_ENFORCE,
        grants=grants_for_agent(agent_id, workspace),
        tenant_id=getattr(ctx, "tenant_id", "") or DEFAULT_TENANT,
    )
    env = dict(built.env)
    # Set AFTER the scrub, so nothing an inherited PYTHONPATH pointed at can
    # ride in. The interpreter runs with -I anyway; this is what the boot
    # script reads to find its own directory and the socket.
    env["GENUS_TOOLS_DIR"] = str(tools_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _spill(workspace: Path, stdout: str) -> str:
    """Full stdout to a file the agent can page back, or "" if it cannot be written.

    The same trade the `analyze_image` batch makes: the model reads a bounded
    head and is told where the rest is, so truncation becomes pagination rather
    than the invisible amputation `exec` used to do at 4,000 characters.
    """
    try:
        root = workspace / WORKDIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"stdout-{uuid.uuid4().hex[:12]}.txt"
        path.write_text(stdout, encoding="utf-8")
        return str(path)
    except OSError as exc:
        logger.warning("execute_code could not spill stdout: %s", exc)
        return ""


def _kill_group(pgid: int) -> None:
    """Kill the snippet's whole process group. Never raises.

    `start_new_session=True` makes the child a session leader, so its process
    GROUP id equals its pid and everything it starts inherits that group. This
    is what reaches a `subprocess.Popen` the snippet left running after it
    returned — exactly the leak the `exec` handler warns the model about and
    had no way to enforce.

    The caller passes the pgid it captured at SPAWN time, never
    ``os.getpgid(pid)`` at kill time: once the child has been waited on it is
    reaped, `getpgid` raises, and the suppression below would swallow the
    failure and let the orphan live. That is how the first cut of this
    function passed its own test on the timeout path and failed it on the
    success path.

    The self-check is not paranoia about a value we computed — it is a check
    that ``start_new_session`` did what it said. If it ever silently did not,
    this would be the engine killing its own process group.
    """
    if pgid <= 0 or pgid == os.getpgid(0):
        logger.error("execute_code refused to kill process group %s — it is our own", pgid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)


@_handler("execute_code")
async def _execute_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.code_exec_rpc import ToolRpcServer
    from robothor.engine.tool_proxy import get_tool_proxy

    code = args.get("code") or ""
    if not isinstance(code, str) or not code.strip():
        return {"error": "No code provided. Pass the Python source as `code`."}
    if len(code) > MAX_CODE_CHARS:
        return {
            "error": (
                f"Snippet is {len(code)} characters, over the {MAX_CODE_CHARS} limit. "
                "Write the data to a file and have the snippet read it."
            )
        }

    proxy = get_tool_proxy()
    if proxy is None:
        return {
            "error": (
                "execute_code is only available inside an agent run — there is no "
                "tool proxy on this call."
            )
        }

    if "exec" not in getattr(proxy, "allowed", frozenset()):
        return {
            "error": (
                "execute_code needs the `exec` capability, and this agent's manifest "
                "does not grant it. Ask the operator to add `exec` to tools_allowed."
            )
        }

    refusal = _refuse_if_sandboxed()
    if refusal:
        return refusal

    workspace = Path(getattr(ctx, "workspace", "") or Path.cwd())
    timeout = _resolved_timeout(args, ctx)
    # The cap the PROXY carries, not one this handler reads for itself: a
    # limit a tool looks up on its own behalf is a limit the tool can be
    # persuaded to look up differently.
    max_calls = max(1, int(getattr(proxy, "max_calls", DEFAULT_MAX_TOOL_CALLS)))
    stdout_cap = _settings_int("execute_code_max_output", MAX_STDOUT_BYTES)

    # The per-call directory is a TEMP directory, not a workspace one, for a
    # reason that is a hard kernel limit rather than taste: an AF_UNIX path may
    # be ~108 bytes, and a workspace two directories deep plus `.robothor/
    # execute_code/<id>/rpc.sock` exceeds it on ordinary machines. (It could
    # live in the workspace if a container had to reach it — but a
    # container-sandboxed agent is refused above, so nothing needs it there.)
    # The SPILL still goes under the workspace, where the agent can read it.
    tools_dir = Path(tempfile.mkdtemp(prefix="genus-code-"))
    server = ToolRpcServer(directory=tools_dir, proxy=proxy, max_calls=max_calls)
    try:
        await server.start()
        _stage(tools_dir, code)
        result = await _run_snippet(
            tools_dir=tools_dir,
            workspace=workspace,
            env=_child_environment(ctx, tools_dir),
            timeout=timeout,
            hard_cap=stdout_cap * HARD_CAP_MULTIPLIER,
        )
    except OSError as exc:
        return {"error": f"execute_code could not start: {exc}"}
    finally:
        await server.aclose()
        shutil.rmtree(tools_dir, ignore_errors=True)

    return _shape(result, server=server, workspace=workspace, stdout_cap=stdout_cap)


def _refuse_if_sandboxed() -> dict[str, Any] | None:
    """A containerised agent is refused, and told why.

    An agent declaring `sandbox: docker` runs its commands inside a container
    with `--network none` and no host environment. The tool proxy lives in the
    engine process, so a snippet in that container could not reach it — and
    running the snippet on the HOST instead would silently undo the isolation
    the manifest asked for. Refusing names the trade; falling back would hide
    it, which is the failure this codebase keeps recording.
    """
    from robothor.engine.sandbox import SandboxMode, get_current_sandbox

    sandbox = get_current_sandbox()
    if sandbox is None or sandbox.mode == SandboxMode.LOCAL:
        return None
    return {
        "error": (
            "execute_code is not available to a container-sandboxed agent: the "
            "tool proxy runs in the engine and the container has no route to it. "
            "Use `exec` for shell work, or call the tools directly from a turn."
        )
    }


def _resolved_timeout(args: dict[str, Any], ctx: ToolContext) -> int:
    """The snippet's ceiling, then the RUN's — a snippet may not outlive its run."""
    from robothor.engine.run_pacing import clamp_tool_timeout, mode_for_run

    default = _settings_int("execute_code_timeout", DEFAULT_TIMEOUT_SECONDS)
    run_id = getattr(ctx, "run_id", "") or ""
    timeout, _note = clamp_tool_timeout(
        resolve_timeout(args, default), mode=mode_for_run(run_id), run_id=run_id
    )
    return max(1, int(timeout))


def _stage(tools_dir: Path, code: str) -> None:
    """Write the three files the snippet runs from."""
    from robothor.engine.sandbox_runtime import genus_tools as _client

    client_source = Path(_client.__file__).read_text(encoding="utf-8")
    (tools_dir / "genus_tools.py").write_text(client_source, encoding="utf-8")
    (tools_dir / "snippet.py").write_text(code, encoding="utf-8")
    (tools_dir / "_boot.py").write_text(
        _BOOT_TEMPLATE.format(guarded=GUARDED_IMPORTS), encoding="utf-8"
    )


async def _drain(stream: Any, sink: list[bytes], hard_cap: int) -> None:
    """Read one pipe, stopping at the hard cap rather than at EOF.

    The cap is applied WHILE reading, not after. A snippet printing fifty
    megabytes must not get the engine to hold fifty megabytes first and decide
    afterwards that it was too much — buffering-then-capping is the hole the
    plugin scanner review recorded on 2026-09-15, in a different file.
    """
    held = 0
    while held < hard_cap:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink.append(chunk)
        held += len(chunk)
    # Past the cap: keep draining so the child is not blocked on a full pipe,
    # and keep none of it.
    while await stream.read(65536):
        pass


async def _wait_for_exit(proc: Any, timeout: float) -> bool:
    """Wait for the SNIPPET to exit. True if it did, False on the deadline.

    Polls ``proc.returncode`` rather than awaiting ``proc.wait()``, and the
    difference is the whole point. asyncio's subprocess transport only finishes
    ``wait()`` once the process has exited AND every pipe it handed out has
    closed — and a child the snippet backgrounded inherited stdout, so it holds
    one open. Awaiting ``wait()`` therefore keeps the tool call alive for as
    long as the orphan runs, which is the opposite of the property this tool
    promises. ``returncode`` is set by the child watcher the moment the process
    itself exits, independent of any pipe.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while proc.returncode is None:
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(EXIT_POLL_SECONDS)
    return True


async def _run_snippet(
    *, tools_dir: Path, workspace: Path, env: dict[str, str], timeout: int, hard_cap: int
) -> SandboxResult:
    """Spawn, wait, and make sure nothing survives."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        str(tools_dir / "_boot.py"),
        cwd=str(workspace) if workspace.is_dir() else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # Captured now, not at kill time: once the child is waited on it is reaped
    # and `os.getpgid` raises, which would leave anything it backgrounded
    # running while the kill looked like it had happened.
    pgid = proc.pid
    out: list[bytes] = []
    err: list[bytes] = []

    # The pipes are drained CONCURRENTLY with the wait, and the wait is on the
    # snippet's own exit — not on the pipes closing. Those are different
    # events, and conflating them is a bug in both directions:
    #
    #   * waiting for exit without draining deadlocks the moment the snippet
    #     prints more than the pipe buffer (64 KiB) and blocks on a reader that
    #     is not reading;
    #   * waiting for the pipes to CLOSE keeps this call alive for as long as
    #     anything the snippet backgrounded holds the inherited stdout — which
    #     is to say, a daemon the snippet started holds the tool call open. The
    #     first cut did that and its own "nothing survives" test passed for the
    #     wrong reason: the handler had simply waited for the orphan to finish.
    drains = asyncio.gather(
        _drain(proc.stdout, out, hard_cap),
        _drain(proc.stderr, err, hard_cap),
    )

    timed_out = not await _wait_for_exit(proc, timeout)
    # On BOTH paths. A snippet that returned after backgrounding a child leaves
    # that child in this group, and "it finished" is not the same as "nothing it
    # started is still running". Killing the group also closes the inherited
    # pipe ends, which is what lets the drains below reach EOF rather than wait
    # on an orphan.
    _kill_group(pgid)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(drains, timeout=DRAIN_GRACE_SECONDS)
    drains.cancel()
    with contextlib.suppress(Exception):
        await proc.wait()

    return SandboxResult(
        stdout=b"".join(out).decode("utf-8", errors="replace"),
        stderr=b"".join(err).decode("utf-8", errors="replace"),
        returncode=-1 if timed_out else (proc.returncode if proc.returncode is not None else -1),
        tool_call_count=0,
        timed_out=timed_out,
    )


def _shape(
    result: SandboxResult, *, server: Any, workspace: Path, stdout_cap: int
) -> dict[str, Any]:
    """The result the model reads: bounded, marked, and never silently cut."""
    full_stdout = result.stdout
    stdout, stdout_cut = truncate_with_marker(full_stdout, stdout_cap)
    stderr, stderr_cut = truncate_with_marker(result.stderr, MAX_STDERR_BYTES)

    shaped = SandboxResult(
        stdout=stdout,
        stderr=stderr,
        returncode=result.returncode,
        tool_call_count=server.calls_served,
        timed_out=result.timed_out,
        stdout_truncated=stdout_cut,
        stderr_truncated=stderr_cut,
    ).as_dict()

    if stdout_cut:
        path = _spill(workspace, full_stdout)
        if path:
            shaped["stdout_file"] = path
            shaped["note"] = (
                f"Output was {len(full_stdout)} characters; the first {stdout_cap} are "
                f"above and the whole of it is at {path}. Work over that file "
                "rather than re-running the snippet to see the rest."
            )
    if result.timed_out:
        shaped["error"] = (
            "The snippet ran out of time and its process group was killed. Ask for "
            f"more with the `timeout` parameter (up to {MAX_TIMEOUT_SECONDS}s), or do "
            "less per snippet — nothing it backgrounded survived."
        )
    if server.calls_served >= server.max_calls:
        shaped["tool_call_limit_reached"] = True
    return shaped
