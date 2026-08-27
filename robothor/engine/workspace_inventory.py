"""Tell an agent what is in the workspace it was pointed at.

An agent starts knowing nothing about its own files. On WildClawBench
task_3 the workspace holds sixteen files, all images, across four
directories, and the run opens by groping: directory listings, then a
`view_image` per picture, before any reasoning begins. OpenClaw scores 1.0
on that task, and the comment beside `view_image` in the bench manifest
already names why —

    the competing harness reads images into context by default, which is
    how it scored where we scored zero on the same multimodal model

The same shape cost a skills task 11 directory listings and 7 file reads
before it failed.

This does not auto-attach anything. It states what exists, so the turns go
on the problem instead of on discovery. Images are called out by name
because a picture the agent never thinks to open is a picture it cannot
reason about.

Off unless a manifest asks for it. An operator's workspace is not a
benchmark fixture — listing a home directory would be neither small nor
useful — so this is opt-in per agent rather than a fleet default.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

#: Entries listed before truncation. Enough to describe a task workspace,
#: small enough that it never crowds out the instruction itself.
DEFAULT_LIMIT = 60

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})

#: Directories whose contents describe the tooling, not the task.
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"})


def workspace_inventory(workspace: str | Path | None, limit: int = DEFAULT_LIMIT) -> str:
    """A short listing of the workspace, images flagged. "" when there is nothing.

    Never raises: a preamble that can fail is a preamble that takes the run
    with it, and this is a convenience rather than a control.
    """
    if not workspace:
        return ""
    try:
        root = Path(workspace)
        if not root.is_dir():
            return ""
        entries: list[str] = []
        images = 0
        total = 0
        for path in sorted(root.rglob("*")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            total += 1
            if len(entries) >= limit:
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() in _IMAGE_SUFFIXES:
                images += 1
                entries.append(f"  {rel}  [image]")
            else:
                entries.append(f"  {rel}")
    except OSError as exc:
        logger.debug("workspace inventory skipped: %s", exc)
        return ""

    if not entries:
        return ""

    header = f"Files in your workspace ({total}"
    header += f", {images} image{'s' if images != 1 else ''}" if images else ""
    header += "):"
    body = "\n".join(entries)
    if total > len(entries):
        body += f"\n  … and {total - len(entries)} more — list the directory for the rest"
    if images:
        body += "\n  Use view_image to look at an image; you cannot read one with read_file."
    return f"{header}\n{body}"


def inventory_context_hook(config: AgentConfig) -> str | None:
    """Warmup hook. Silent unless the manifest sets `v2.workspace_inventory`."""
    try:
        v2: dict[str, Any] = getattr(config, "v2", None) or {}
        if not v2.get("workspace_inventory"):
            return None
        # AgentConfig carries no workspace field; the runner resolves it the
        # same way, agent-first then the instance default. Never a hardcoded
        # path — rule 2.
        import os

        root = getattr(config, "workspace", "") or os.environ.get("ROBOTHOR_WORKSPACE", "")
        text = workspace_inventory(root)
        return text or None
    except Exception as exc:  # noqa: BLE001 - warmup must never fail on this
        logger.debug("workspace inventory hook failed: %s", exc)
        return None


def register() -> None:
    """Wire the hook into warmup. Idempotent."""
    from robothor.engine import warmup

    if any(
        getattr(h, "__name__", "") == "inventory_context_hook"
        for h in warmup._AGENT_CONTEXT_HOOKS  # noqa: SLF001 - the registry is module state
    ):
        return
    warmup.register_agent_context_hook(inventory_context_hook)
