"""Filesystem tool handlers — exec, read_file, write_file, list_directory."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


#: Seconds an `exec` gets when the agent does not ask for more. Unchanged —
#: most commands are short and a long default would hide hangs.
DEFAULT_EXEC_TIMEOUT = 30

#: The most an agent may ask for. A command that outlives the run owning it
#: is a leak, not a long job; 15 minutes covers model calls, builds and
#: media work while staying inside every agent's wall-clock ceiling.
MAX_EXEC_TIMEOUT = 900


def resolve_exec_timeout(args: dict[str, Any]) -> int:
    """The timeout for one exec call: what was asked for, within the ceiling.

    Tolerant of what models actually emit — `"120"` as often as `120` — and
    falls back rather than raising, because a malformed timeout must not turn
    a working command into a tool error.
    """
    raw = args.get("timeout", DEFAULT_EXEC_TIMEOUT)
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXEC_TIMEOUT
    if requested <= 0:
        return DEFAULT_EXEC_TIMEOUT
    return min(requested, MAX_EXEC_TIMEOUT)


@_handler("exec")
async def _exec(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = args.get("command", "")
    if not command:
        return {"error": "No command provided"}

    # Before the sandbox decision, so host and container paths refuse alike:
    # a command that PRINTS a secrets file (or the environment) never runs.
    # Sourcing the file to run an authenticated command is still allowed.
    from robothor.engine.secret_paths import exec_reads_secret

    refused = exec_reads_secret(command)
    if refused:
        return {"error": refused}

    # The tool's own ceiling, then the RUN's: a command may not outlive the run
    # that owns it, and it must leave time for the write the run is graded on.
    # Every exec in the profiled 1200s failure asked for 900s.
    # The run id makes the observe-rung line attributable — evidence that cannot
    # be tied to a run is not evidence — and the mode comes from the run's
    # cached rung rather than a DB-backed flag read per `exec`, which the
    # profiled run made 41 times.
    from robothor.engine.run_pacing import clamp_tool_timeout, mode_for_run

    run_id = getattr(ctx, "run_id", "") or ""
    timeout, clamp_note = clamp_tool_timeout(
        resolve_exec_timeout(args), mode=mode_for_run(run_id), run_id=run_id
    )

    def _with_note(result: dict[str, Any]) -> dict[str, Any]:
        """Say when the timeout the agent asked for is not the one it got."""
        if clamp_note and isinstance(result, dict):
            result["timeout_note"] = clamp_note
        return result

    # An agent configured `sandbox: docker` must actually have its shell
    # commands run in the container. This used to go straight to subprocess.run
    # on the host, so the sandbox setting was decoration: only browser/desktop
    # routed into the container, and no sandboxed agent uses those tools.
    #
    # Fail closed: if the container is active but unusable, surface the error
    # rather than quietly running the command on the host (#201).
    from robothor.engine.sandbox import SandboxMode, get_current_sandbox

    sandbox = get_current_sandbox()
    if sandbox is not None and sandbox.mode != SandboxMode.LOCAL:
        # A container never inherits the engine's environment in the first
        # place — a stricter isolation than the host scrub below, and the
        # reason this branch needs none. It also means a `secrets:` grant does
        # not reach the container, so say so rather than letting the agent
        # discover it as an unexplained failure.
        from robothor.engine.exec_env import grants_for_agent as _grants

        try:
            result = await sandbox.exec_shell(command, timeout=timeout)
        except Exception as e:
            return {"error": f"Sandboxed exec failed: {e}"}
        sandboxed_grants = _grants(
            getattr(ctx, "agent_id", "") or "", getattr(ctx, "workspace", "") or ""
        )
        if sandboxed_grants and isinstance(result, dict):
            result["secret_grant_note"] = (
                "Secret grants — this agent runs sandboxed, and a container "
                "receives none of the host's environment, so "
                f"{', '.join(sandboxed_grants)} were not available to the "
                "command."
            )
        return _with_note(result)

    # The child's environment is built from an allowlist rather than inherited.
    # Without this, `subprocess.run` with no `env=` handed every shell command
    # an agent asked for the engine's whole process environment — the ~50
    # credentials decrypted out of a root-owned SOPS file at boot — to any
    # exec-capable agent, sub-agents included. `secret_paths` refuses the
    # commands that PRINT a secrets file; it cannot help with a command that
    # simply USES $GITHUB_TOKEN, so the fix is that the token is not there.
    #
    # The grant is looked up by THIS context's agent id, which for a spawned
    # sub-agent is the child's — so a sub-agent reads its own manifest and
    # inherits nothing.
    from robothor.engine.exec_env import build_exec_env, grants_for_agent

    # `getattr` rather than attribute access: this runs on EVERY exec, and a
    # caller passing a narrower context than the full ToolContext must lose a
    # grant lookup, never the scrub. Losing the scrub is a credential leak;
    # losing the lookup is a missing grant, which is noted.
    exec_agent_id = getattr(ctx, "agent_id", "") or ""
    exec_workspace = getattr(ctx, "workspace", "") or ""
    child_env = await asyncio.to_thread(
        lambda: build_exec_env(
            agent_id=exec_agent_id,
            base=None,
            grants=grants_for_agent(exec_agent_id, exec_workspace),
            tenant_id=getattr(ctx, "tenant_id", "") or DEFAULT_TENANT,
        )
    )

    def _with_grant_note(result: dict[str, Any]) -> dict[str, Any]:
        """Say when a granted credential was refused or was simply not there.

        A silent absence is the failure shape this codebase keeps re-learning:
        the command fails for an unrelated-looking reason and nothing anywhere
        says the credential was never present.
        """
        if child_env.note and isinstance(result, dict):
            result["secret_grant_note"] = child_env.note
        return result

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=ctx.workspace or None,
                env=child_env.env,
            )
            return {
                "stdout": proc.stdout[:4000],
                "stderr": proc.stderr[:2000],
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            # The limit rides in its OWN field, never inside the message. When
            # the run's remaining budget is clamping this call, that number
            # changes every call — and the repeat guard digests `error`, so a
            # limit baked into the text put the wall clock back inside the
            # digest and made six identical timeouts look like six different
            # results. The agent still reads the number: it is in the result.
            return {
                "error": (
                    "Command timed out. Ask for more time with the `timeout` "
                    f"parameter (up to {MAX_EXEC_TIMEOUT}s) rather than "
                    "backgrounding the command — a backgrounded child is "
                    "killed when exec returns."
                ),
                "timeout_seconds": timeout,
            }
        except Exception as e:
            return {"error": f"Command failed: {e}"}

    return _with_grant_note(_with_note(await asyncio.to_thread(_run)))


_SEARCH_SKIP_DIRS = frozenset(
    {
        ".git",
        "venv",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
_SEARCH_MAX_FILE_BYTES = 2_000_000


@_handler("search_files")
async def _search_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search file CONTENTS by regex (first-party, pure-Python, no shell-out).

    Workspace-scoped, prunes heavy dirs (.git/venv/node_modules/...). Returns
    {file, line, text} matches. Use this instead of shelling out via exec to find
    code — it is the self-improvement loop's code-search surface.
    """
    import fnmatch
    import os
    import re as _re
    from pathlib import Path

    pattern = args.get("pattern", "")
    if not pattern:
        return {"error": "No pattern provided"}
    glob = args.get("glob") or ""
    max_results = int(args.get("max_results", 100))
    root = Path(ctx.workspace).resolve() if ctx.workspace else Path.cwd()
    base = (root / (args.get("path") or ".")).resolve()
    try:  # keep the search inside the workspace
        base.relative_to(root)
    except ValueError:
        return {"error": "path escapes the workspace"}
    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return {"error": f"invalid regex: {e}"}

    def _rel(fp: Path) -> str:
        try:
            return str(fp.relative_to(root))
        except ValueError:
            return str(fp)

    def _scan_file(fp: Path, matches: list[dict[str, Any]]) -> bool:
        """Append matches; return True when max_results reached."""
        try:
            # Resolve symlinks and confirm the real target is still inside the
            # workspace. The relative_to guard on `base` only checks the symlink's
            # own path, so a symlinked file pointing outside the workspace would
            # otherwise have its contents read and leaked to the agent.
            if not fp.resolve().is_relative_to(root):
                return False
            if fp.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                return False
            with fp.open("r", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append({"file": _rel(fp), "line": i, "text": line.rstrip()[:300]})
                        if len(matches) >= max_results:
                            return True
        except (OSError, UnicodeDecodeError):
            return False
        return False

    def _run() -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        if base.is_file():
            hit = _scan_file(base, matches)
            return {"matches": matches, "count": len(matches), "truncated": hit}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS]
            for fn in sorted(filenames):
                if glob and not fnmatch.fnmatch(fn, glob):
                    continue
                if _scan_file(Path(dirpath) / fn, matches):
                    return {"matches": matches, "count": len(matches), "truncated": True}
        return {"matches": matches, "count": len(matches), "truncated": False}

    return await asyncio.to_thread(_run)


@_handler("read_file")
async def _read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        from robothor.engine.secret_paths import is_secret_path, refusal_for

        path = Path(args.get("path", "")).expanduser()
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
        # Before touching the filesystem: the refusal must not reveal whether
        # the file exists, and a credential file is never opened for a model
        # (see robothor/engine/secret_paths.py for the live reads behind this).
        if is_secret_path(path):
            return {"error": refusal_for(path)}
        # The same question for a file the OPERATOR sent over a channel.
        # `secret_paths` cannot answer it: the inbox stores a sanitised name and
        # `.env` sanitises to `env`, so the rules see nothing to object to. The
        # verdict was taken at save time and recorded as the directory the file
        # sits in; this is the one additive call site that consults it. Without
        # it, `read_file` on the inbox copy put the raw credential into the
        # model's context — and nothing downstream redacts a TOOL RESULT.
        from robothor.engine.attachments import is_inbox_secret

        if is_inbox_secret(path):
            return {"error": refusal_for(path)}
        try:
            content = path.read_text()
            return {"content": content[:50000], "path": str(path), "chars": len(content)}
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}

    return await asyncio.to_thread(_run)


@_handler("list_directory")
async def _list_directory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        path = Path(args.get("path", "")).expanduser()
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
        if not path.exists():
            return {"error": f"Path does not exist: {path}"}
        if not path.is_dir():
            return {"error": f"Not a directory: {path}"}
        try:
            pattern = args.get("pattern", "")
            recursive = args.get("recursive", False)
            entries = []
            max_entries = 200
            if pattern:
                gen = path.rglob(pattern) if recursive else path.glob(pattern)
                for p in gen:
                    entries.append(
                        {
                            "name": str(p.relative_to(path)),
                            "type": "dir" if p.is_dir() else "file",
                            "size": p.stat().st_size if p.is_file() else 0,
                        }
                    )
                    if len(entries) >= max_entries:
                        break
            else:
                for p in sorted(path.iterdir()):
                    entries.append(
                        {
                            "name": p.name,
                            "type": "dir" if p.is_dir() else "file",
                            "size": p.stat().st_size if p.is_file() else 0,
                        }
                    )
                    if len(entries) >= max_entries:
                        break
            truncated = len(entries) >= max_entries
            return {
                "path": str(path),
                "entries": entries,
                "count": len(entries),
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": f"Failed to list directory: {e}"}

    return await asyncio.to_thread(_run)


@_handler("write_file")
async def _write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        path = Path(args.get("path", "")).expanduser()
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return {"success": True, "path": str(path)}
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

    return await asyncio.to_thread(_run)
