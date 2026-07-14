"""Filesystem tool handlers — exec, read_file, write_file, list_directory."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("exec")
async def _exec(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = args.get("command", "")
    if not command:
        return {"error": "No command provided"}

    timeout = int(args.get("timeout", 30))

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
        try:
            return await sandbox.exec_shell(command, timeout=timeout)
        except Exception as e:
            return {"error": f"Sandboxed exec failed: {e}"}

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=int(args.get("timeout", 30)),
                cwd=ctx.workspace or None,
            )
            return {
                "stdout": proc.stdout[:4000],
                "stderr": proc.stderr[:2000],
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out ({int(args.get('timeout', 30))}s limit)"}
        except Exception as e:
            return {"error": f"Command failed: {e}"}

    return await asyncio.to_thread(_run)


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
        path = Path(args.get("path", "")).expanduser()
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
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
