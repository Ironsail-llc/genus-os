"""Git tool handlers — status, diff, branch, commit, push, create_pull_request."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import PROTECTED_BRANCHES

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("git_status")
async def _git_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "-b"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            return {"status": proc.stdout.strip(), "exit_code": proc.returncode}
        except Exception as e:
            return {"error": f"git status failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("git_diff")
async def _git_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None
    staged = args.get("staged", False)

    def _run() -> dict[str, Any]:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            return {"diff": proc.stdout[:20000], "exit_code": proc.returncode}
        except Exception as e:
            return {"error": f"git diff failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("git_branch")
async def _git_branch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None
    branch_name = args.get("branch_name", "")

    def _run() -> dict[str, Any]:
        if not branch_name:
            return {"error": "branch_name is required"}
        if branch_name in PROTECTED_BRANCHES:
            return {"error": f"Cannot create/switch to protected branch: {branch_name}"}
        try:
            proc = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"git checkout -b failed: {proc.stderr.strip()}"}
            return {"branch": branch_name, "created": True}
        except Exception as e:
            return {"error": f"git branch failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("git_commit")
async def _git_commit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None
    message = args.get("message", "")
    files = args.get("files", [])

    def _run() -> dict[str, Any]:
        if not message:
            return {"error": "commit message is required"}

        # Check current branch — reject commits on protected branches
        try:
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            current_branch = branch_proc.stdout.strip()
            if current_branch in PROTECTED_BRANCHES:
                return {"error": f"Cannot commit on protected branch: {current_branch}"}
        except Exception:
            pass  # proceed — branch check is best-effort

        try:
            # Stage files
            if files:
                stage_proc = subprocess.run(
                    ["git", "add"] + files,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
            else:
                stage_proc = subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
            if stage_proc.returncode != 0:
                return {"error": f"git add failed: {stage_proc.stderr.strip()}"}

            # Commit
            commit_proc = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=repo_path,
            )
            if commit_proc.returncode != 0:
                return {"error": f"git commit failed: {commit_proc.stderr.strip()}"}

            # Get commit hash
            hash_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            return {
                "committed": True,
                "message": message,
                "sha": hash_proc.stdout.strip()[:12],
                "output": commit_proc.stdout.strip()[:1000],
            }
        except Exception as e:
            return {"error": f"git commit failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("git_push")
async def _git_push(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None
    set_upstream = args.get("set_upstream", True)

    def _run() -> dict[str, Any]:
        # Check current branch — reject push on protected branches
        try:
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            current_branch = branch_proc.stdout.strip()
            if current_branch in PROTECTED_BRANCHES:
                return {"error": f"Cannot push to protected branch: {current_branch}"}
        except Exception as e:
            return {"error": f"Failed to determine current branch: {e}"}

        try:
            cmd = ["git", "push"]
            if set_upstream:
                cmd.extend(["-u", "origin", current_branch])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"git push failed: {proc.stderr.strip()}"}
            return {"pushed": True, "branch": current_branch, "output": proc.stdout.strip()[:1000]}
        except Exception as e:
            return {"error": f"git push failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("create_pull_request")
async def _create_pull_request(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo_path = args.get("path") or ctx.workspace or None
    title = args.get("title", "")
    body = args.get("body", "")
    base = args.get("base", "main")
    labels = args.get("labels", [])

    def _run() -> dict[str, Any]:
        if not title:
            return {"error": "PR title is required"}

        # Always add 'nightwatch' label
        all_labels = list(set(["nightwatch"] + labels))
        label_arg = ",".join(all_labels)

        try:
            cmd = [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base,
                "--label",
                label_arg,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"gh pr create failed: {proc.stderr.strip()}"}
            pr_url = proc.stdout.strip()
            return {"created": True, "url": pr_url, "title": title, "draft": True}
        except Exception as e:
            return {"error": f"create_pull_request failed: {e}"}

    return await asyncio.to_thread(_run)
