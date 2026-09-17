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
  provider key, no channel token — which is why `/proc/SELF/environ` is a probe
  with a boring answer here.
* **a hardened engine, checked rather than assumed.** Scrubbing removes
  INHERITANCE, not the credentials from the engine — and the snippet's parent
  IS the engine, whose `/proc/<pid>/environ` a same-uid process may read
  (measured: one line, all nine seeded credentials). `code_exec_guards.
  harden_this_process` closes that before the spawn. A mitigation, not a
  boundary; see its docstring.
* **an isolated interpreter** (`-I`: no `PYTHONPATH`, no user site, no implicit
  script directory) plus an import guard that refuses the engine's own
  packages. The guard is a guardrail and NOT a boundary — a determined snippet
  can remove a meta-path finder — and it does not need to be one: the boundary
  is that the environment holds nothing worth importing the engine for.
* **its own process group, plus a census of its descendants**, killed on the
  way out whether the snippet finished, timed out or was cancelled. The census
  is taken WHILE the snippet runs: on the ordinary exit path its children have
  already reparented by the time we kill. What survives: a child started with
  `start_new_session=True` whose parent then calls `os._exit`, skipping the
  reaper's `finally` and leaving the census nothing to have sampled. A snippet
  FORCING the escape, reproducibly — not winning a race. Only a cgroup the
  engine can kill as a unit closes it, which needs `Delegate=yes` on the unit:
  an operator change. Every place that describes this says so.

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

import contextlib
import logging
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.code_exec_process import (
    MAX_TIMEOUT_SECONDS,
    PR_SET_CHILD_SUBREAPER,
    run_snippet,
)
from robothor.engine.code_exec_result import HARD_CAP_MULTIPLIER, recorded_http_calls, shape
from robothor.engine.code_execution import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDOUT_BYTES,
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

#: The module name the outbound-HTTP recorder is staged under, beside
#: `genus_tools`. Underscored so it shadows nothing a snippet would import.
HTTP_RECORDER_MODULE = "_genus_http_recorder.py"


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


def _run_scratch_root(ctx: ToolContext) -> str:
    """A 0700 directory per run, under the engine's temp directory.

    Attribution, not isolation: a leftover directory says which run left it,
    and the run's own calls are together. The reach control is the peer-session
    check on the socket — see `code_exec_rpc` — because 0700 owned by the uid
    every snippet runs as excludes nobody that matters.
    """
    run_id = "".join(c for c in (getattr(ctx, "run_id", "") or "") if c.isalnum())[:16]
    root = Path(tempfile.gettempdir()) / f"genus-run-{run_id or 'unattributed'}"
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(stat.S_IRWXU)
    return str(root)


@_handler("execute_code")
async def _execute_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.code_exec_guards import harden_this_process, preflight
    from robothor.engine.code_exec_rpc import ToolRpcServer
    from robothor.engine.tool_proxy import get_tool_proxy

    proxy = get_tool_proxy()
    refusal = preflight(args.get("code") or "", proxy)
    if refusal is not None or proxy is None:
        # `preflight` already refuses a None proxy; the second half of this
        # condition says so to the type checker, which cannot read that from
        # the other module.
        return refusal or {"error": "execute_code is only available inside an agent run."}
    # After the refusals and before the spawn: nothing exists yet that could
    # read this process, and everything after this line can.
    harden_this_process()

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
    #
    # Grouped per RUN so an operator reading /tmp can attribute a leftover, and
    # 0700 at both levels — but neither of those is the control. Every snippet
    # on this box runs as the engine uid, so file permissions exclude nobody
    # that matters; `ToolRpcServer.bind_to_session` is what stops one run's
    # snippet driving another run's proxy.
    code = str(args.get("code") or "")
    # Where this snippet's proxied calls start in the turn's ledger. A turn may
    # hold two `execute_code` calls in sequence, and the second one's count of
    # unread responses must not inherit the first one's.
    responses_from = len(getattr(proxy, "responses", ()))
    tools_dir = Path(tempfile.mkdtemp(prefix="code-", dir=_run_scratch_root(ctx)))
    server = ToolRpcServer(directory=tools_dir, proxy=proxy, max_calls=max_calls)
    http_calls: list[dict[str, Any]] | None = None
    http_recorder = ""
    try:
        await server.start()
        _stage(tools_dir, code)
        result = await run_snippet(
            tools_dir=tools_dir,
            workspace=workspace,
            env=_child_environment(ctx, tools_dir),
            timeout=timeout,
            hard_cap=stdout_cap * HARD_CAP_MULTIPLIER,
            on_spawn=server.bind_to_session,
        )
        # Read before the `finally` removes the directory. Best effort in the
        # same sense as the recorder: a record that cannot be read is no
        # record, the snippet's own result is unchanged by it, and the result
        # SAYS so rather than looking like a snippet that made no request.
        try:
            http_calls = recorded_http_calls(tools_dir)
        except Exception as exc:  # noqa: BLE001 - diagnostics never fail the call
            logger.warning("execute_code: the HTTP record could not be read: %r", exc)
            http_recorder = "unreadable"
    except OSError as exc:
        return {"error": f"execute_code could not start: {exc}"}
    finally:
        # `BaseException`, deliberately: on the cancellation path `aclose()`
        # awaits, so a second cancel can land inside it — and skipping the
        # rmtree would leak the directory holding this call's token. The
        # original exception still propagates after this block; all that is
        # swallowed is a failure to clean up.
        try:
            await server.aclose()
        except BaseException as exc:  # noqa: BLE001 - cleanup must not be skipped
            logger.warning("execute_code: closing the tool socket failed: %r", exc)
        shutil.rmtree(tools_dir, ignore_errors=True)

    shaped = shape(
        result,
        server=server,
        workspace=workspace,
        stdout_cap=stdout_cap,
        http_calls=http_calls,
        http_recorder=http_recorder,
    )
    # A call's response is EVIDENCE, not a receipt — a proxied call's, and
    # equally one the snippet made on its own. The first measured run printed
    # only each proxied send's `status`; the rerun sent everything through
    # `urllib`, printed `OK`, and the proxied count was an honest zero while
    # three follow-up messages went unread in exactly the same way. Both kinds
    # are counted here, under one rule.
    from robothor.engine.act_observe import raw_http_responses, unread_proxy_responses

    proxied = list(getattr(proxy, "responses", ()))[responses_from:]
    raw = raw_http_responses(http_calls or [])
    shaped.update(unread_proxy_responses(proxied + raw, result.stdout))
    return shaped


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
    """Write the four files the snippet runs from.

    The client is located by PATH, not imported: ``sandbox_runtime`` holds code
    that runs inside a sandbox, and importing it here would execute its
    module-level environment read in the ENGINE — which is exactly what its own
    docstring says it never does. The recorder is copied the same way; only its
    three constants are ever imported engine-side.
    """
    from robothor.engine.sandbox_runtime.boot_template import BOOT_TEMPLATE
    from robothor.engine.sandbox_runtime.http_recorder import RECORD_FILE

    runtime = Path(__file__).resolve().parents[2] / "sandbox_runtime"
    client_source = (runtime / "genus_tools.py").read_text(encoding="utf-8")
    (tools_dir / "genus_tools.py").write_text(client_source, encoding="utf-8")
    recorder_source = (runtime / "http_recorder.py").read_text(encoding="utf-8")
    (tools_dir / HTTP_RECORDER_MODULE).write_text(recorder_source, encoding="utf-8")
    (tools_dir / "snippet.py").write_text(code, encoding="utf-8")
    (tools_dir / "_boot.py").write_text(
        BOOT_TEMPLATE.format(
            guarded=GUARDED_IMPORTS,
            subreaper=PR_SET_CHILD_SUBREAPER,
            recorder=HTTP_RECORDER_MODULE.removesuffix(".py"),
            record_file=RECORD_FILE,
        ),
        encoding="utf-8",
    )
