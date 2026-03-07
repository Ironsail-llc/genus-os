"""Filesystem tool handlers — exec, read_file, write_file, list_directory."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from typing import Any

from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("exec")
async def _exec(args: dict, ctx: ToolContext) -> dict:
    command = args.get("command", "")
    if not command:
        return {"error": "No command provided"}

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=ctx.workspace or None,
            )
            return {
                "stdout": proc.stdout[:4000],
                "stderr": proc.stderr[:2000],
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out (30s limit)"}
        except Exception as e:
            return {"error": f"Command failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("read_file")
async def _read_file(args: dict, ctx: ToolContext) -> dict:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        path = Path(args.get("path", ""))
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
        try:
            content = path.read_text()
            return {"content": content[:50000], "path": str(path), "chars": len(content)}
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}

    return await asyncio.to_thread(_run)


@_handler("list_directory")
async def _list_directory(args: dict, ctx: ToolContext) -> dict:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        path = Path(args.get("path", ""))
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
async def _write_file(args: dict, ctx: ToolContext) -> dict:
    from pathlib import Path

    def _run() -> dict[str, Any]:
        path = Path(args.get("path", ""))
        if not path.is_absolute() and ctx.workspace:
            path = Path(ctx.workspace) / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return {"success": True, "path": str(path)}
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

    return await asyncio.to_thread(_run)
