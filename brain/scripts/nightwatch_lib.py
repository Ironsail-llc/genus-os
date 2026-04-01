#!/usr/bin/env python3
"""
Shared utilities for Nightwatch scripts (heal, research, build).

Provides worktree management, Claude Code invocation, CRM task helpers,
and memory block access.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure the robothor package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robothor.crm.dal import (
    create_task,
    list_tasks,
    resolve_task,
    update_task,
)
from robothor.memory.blocks import read_block, write_block

logger = logging.getLogger("nightwatch")

REPO_ROOT = Path("/home/philip/robothor")
CLAUDE_BIN = shutil.which("claude") or "/home/philip/.local/bin/claude"
DEFAULT_TIMEOUT = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------

def create_worktree(branch_name: str, base_dir: str = "/tmp") -> Path:
    """Create a git worktree for isolated work.

    Args:
        branch_name: Full branch name (e.g. nightwatch/2026-03-05/fix-foo).
        base_dir: Parent directory for the worktree.

    Returns:
        Path to the worktree directory.
    """
    slug = branch_name.replace("/", "-")
    worktree_path = Path(base_dir) / f"nightwatch-{slug}"

    # Clean up any stale worktree at this path
    if worktree_path.exists():
        cleanup_worktree(worktree_path)

    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Created worktree at %s on branch %s", worktree_path, branch_name)
    return worktree_path


def cleanup_worktree(worktree_path: Path) -> None:
    """Remove a git worktree and clean up."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Fallback: force remove the directory
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Prune stale worktree refs
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    logger.info("Cleaned up worktree at %s", worktree_path)


# ---------------------------------------------------------------------------
# Claude Code invocation
# ---------------------------------------------------------------------------

def invoke_claude_code(
    *,
    cwd: Path,
    prompt: str,
    system_prompt: str,
    allowed_tools: str,
    budget: float,
    timeout: int = DEFAULT_TIMEOUT,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Invoke Claude Code CLI in a worktree and return parsed JSON result.

    Args:
        cwd: Working directory (worktree path).
        prompt: The task prompt.
        system_prompt: System-level instructions.
        allowed_tools: Comma-separated tool spec for --allowedTools.
        budget: Max budget in USD.
        timeout: Timeout in seconds.
        model: Model to use.

    Returns:
        Parsed JSON dict from Claude Code output, or error dict.
    """
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", model,
        "--allowedTools", allowed_tools,
        "--max-budget-usd", str(budget),
        "--output-format", "json",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
    ]

    # Strip CLAUDE_* and ANTHROPIC_API_KEY env vars — the CLI uses its own
    # stored credentials (~/.claude/.credentials.json). Session env vars
    # (e.g. from a parent Claude Code process) would override and break auth.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k != "ANTHROPIC_API_KEY"
    }
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/usr/local/bin")

    logger.info(
        "Invoking Claude Code: model=%s budget=$%.2f timeout=%ds cwd=%s",
        model, budget, timeout, cwd,
    )

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            logger.error("Claude Code failed (rc=%d): %s", result.returncode, result.stderr[:500])
            return {
                "error": f"Claude Code exited with code {result.returncode}",
                "stderr": result.stderr[:1000],
                "stdout": result.stdout[:1000],
            }

        # Parse JSON output
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # Claude Code may output JSON with surrounding text
            # Try to extract the JSON object
            match = re.search(r'\{.*\}', result.stdout, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {
                "result": result.stdout,
                "raw_output": True,
            }

    except subprocess.TimeoutExpired:
        logger.error("Claude Code timed out after %ds", timeout)
        return {"error": f"Claude Code timed out after {timeout}s"}


def extract_pr_url(claude_output: dict) -> str | None:
    """Extract PR URL from Claude Code output.

    Searches through the output for a GitHub PR URL pattern.
    """
    text = json.dumps(claude_output) if isinstance(claude_output, dict) else str(claude_output)
    match = re.search(r'https://github\.com/[^\s"\']+/pull/\d+', text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# CRM task helpers
# ---------------------------------------------------------------------------

def get_tasks(tags: list[str], status: str = "TODO", limit: int = 3) -> list[dict]:
    """Get CRM tasks filtered by tags and status."""
    return list_tasks(tags=tags, status=status, limit=limit)


def resolve_nightwatch_task(task_id: str, resolution: str) -> bool:
    """Resolve a nightwatch task with the given resolution."""
    return resolve_task(task_id, resolution=resolution, agent_id="nightwatch")


def create_nightwatch_task(
    title: str,
    body: str,
    tags: list[str],
    priority: str = "normal",
    assigned_to: str = "main",
) -> str | None:
    """Create a CRM task for nightwatch."""
    return create_task(
        title=title,
        body=body,
        tags=tags,
        priority=priority,
        assigned_to_agent=assigned_to,
        created_by_agent="nightwatch",
    )


# ---------------------------------------------------------------------------
# Memory block helpers
# ---------------------------------------------------------------------------

def read_memory_block(name: str) -> str:
    """Read a memory block and return its content."""
    result = read_block(name)
    return result.get("content", "")


def write_memory_block(name: str, content: str) -> bool:
    """Write content to a memory block."""
    result = write_block(name, content)
    return result.get("success", False)


# ---------------------------------------------------------------------------
# Status file helpers
# ---------------------------------------------------------------------------

def write_status_file(path: str, content: str) -> None:
    """Write content to a status file."""
    full_path = REPO_ROOT / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    logger.info("Wrote status to %s", full_path)


# ---------------------------------------------------------------------------
# Pause/scope checks
# ---------------------------------------------------------------------------

def check_pause_condition(nightwatch_log: str) -> bool:
    """Check if nightwatch should be paused (3 consecutive rejections).

    Returns True if paused (should NOT proceed).
    """
    if "PAUSED" in nightwatch_log:
        return True

    # Look for last 3 PR outcomes
    lines = nightwatch_log.strip().split("\n")
    outcomes = []
    for line in reversed(lines):
        if "outcome:" in line.lower():
            if "rejected" in line.lower():
                outcomes.append("rejected")
            elif "merged" in line.lower():
                outcomes.append("merged")
            elif "modified" in line.lower():
                outcomes.append("modified")
            if len(outcomes) >= 3:
                break

    return len(outcomes) >= 3 and all(o == "rejected" for o in outcomes)


def get_merge_rate(nightwatch_log: str) -> float:
    """Calculate merge rate from nightwatch log.

    Returns merge rate as a float (0.0-1.0). Returns 1.0 if no history.
    """
    lines = nightwatch_log.strip().split("\n")
    merged = 0
    total = 0
    for line in lines:
        if "outcome:" in line.lower():
            total += 1
            if "merged" in line.lower() or "modified" in line.lower():
                merged += 1

    if total < 5:
        # Not enough history — default to config-only scope
        return 0.0
    return merged / total


def determine_scope(merge_rate: float) -> str:
    """Determine allowed change scope based on merge rate.

    Returns: 'config', 'config+instructions', or 'config+instructions+code'
    """
    if merge_rate < 0.5:
        return "config"
    elif merge_rate < 0.7:
        return "config+instructions"
    else:
        return "config+instructions+code"


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(name: str) -> logging.Logger:
    """Set up logging for a nightwatch script."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    return log


def today_str() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def slugify(title: str, max_length: int = 50) -> str:
    """Convert a title to a branch-safe slug."""
    slug = title.lower()
    for prefix in ("[p1] ", "[p2] ", "[p3] ", "[feature] ", "[fix] ", "[test] ", "[cleanup] "):
        slug = slug.removeprefix(prefix)
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in slug)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:max_length]


# ---------------------------------------------------------------------------
# Backlog parsing
# ---------------------------------------------------------------------------

_BACKLOG_RE = re.compile(r"^- \[ \]\s*(?:\[(P[123])\]\s*)?(.*)", re.MULTILINE)
_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def parse_backlog(backlog_path: Path) -> list[dict]:
    """Parse the nightwatch backlog file.

    Returns unchecked items sorted by priority (P1 > P2 > P3), then position.
    Each item: {line_number, priority, text, raw_line}.
    """
    if not backlog_path.exists():
        logger.warning("Backlog file not found: %s", backlog_path)
        return []

    items = []
    for i, line in enumerate(backlog_path.read_text().splitlines(), start=1):
        m = _BACKLOG_RE.match(line)
        if m:
            priority = m.group(1) or "P2"
            text = m.group(2).strip()
            if text:
                items.append({
                    "line_number": i,
                    "priority": priority,
                    "text": text,
                    "raw_line": line,
                })

    items.sort(key=lambda x: (_PRIORITY_ORDER.get(x["priority"], 1), x["line_number"]))
    return items


def check_off_backlog_item(backlog_path: Path, line_number: int, pr_url: str | None = None) -> None:
    """Mark a backlog item as done by checking its checkbox and appending PR URL."""
    lines = backlog_path.read_text().splitlines()
    idx = line_number - 1
    if 0 <= idx < len(lines) and lines[idx].startswith("- [ ]"):
        suffix = f" — {pr_url}" if pr_url else ""
        lines[idx] = lines[idx].replace("- [ ]", "- [x]", 1) + suffix
        backlog_path.write_text("\n".join(lines) + "\n")
        logger.info("Checked off backlog item at line %d", line_number)


# ---------------------------------------------------------------------------
# Self-improvement heuristics (no LLM, no DB required)
# ---------------------------------------------------------------------------

def find_self_improvements(repo_root: Path) -> list[dict]:
    """Scan for lightweight improvement opportunities.

    Checks:
    1. Python files in robothor/engine/ with no corresponding test file
    2. TODO/FIXME/HACK counts by file in engine code
    3. Agent manifests referencing nonexistent instruction files

    Returns list of dicts: {title, description, category, priority}.
    """
    improvements: list[dict] = []

    engine_dir = repo_root / "robothor" / "engine"
    tests_dir = engine_dir / "tests"

    # 1. Untested engine modules
    if engine_dir.exists() and tests_dir.exists():
        test_files = {f.name for f in tests_dir.glob("test_*.py")}
        for py_file in sorted(engine_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            expected_test = f"test_{py_file.name}"
            if expected_test not in test_files:
                improvements.append({
                    "title": f"Add tests for robothor/engine/{py_file.name}",
                    "description": (
                        f"The engine module `robothor/engine/{py_file.name}` has no "
                        f"corresponding test file `robothor/engine/tests/{expected_test}`. "
                        f"Read the module, understand its public API, and write focused "
                        f"unit tests with mocked dependencies."
                    ),
                    "category": "test",
                    "priority": "P2",
                })

    # 2. TODO/FIXME/HACK hotspots in engine code
    if engine_dir.exists():
        marker_re = re.compile(r"#\s*(TODO|FIXME|HACK)\b", re.IGNORECASE)
        file_counts: list[tuple[int, Path]] = []
        for py_file in engine_dir.glob("*.py"):
            try:
                count = len(marker_re.findall(py_file.read_text()))
                if count >= 3:
                    file_counts.append((count, py_file))
            except OSError:
                continue
        file_counts.sort(reverse=True)
        if file_counts:
            count, top_file = file_counts[0]
            improvements.append({
                "title": f"Address {count} TODO/FIXME markers in {top_file.name}",
                "description": (
                    f"`robothor/engine/{top_file.name}` has {count} TODO/FIXME/HACK "
                    f"markers. Read each one, determine if it's still relevant, and "
                    f"either implement the fix or remove the stale marker with a "
                    f"comment explaining why."
                ),
                "category": "quality",
                "priority": "P3",
            })

    # 3. Manifests referencing nonexistent instruction files
    agents_dir = repo_root / "docs" / "agents"
    if agents_dir.exists():
        import yaml  # noqa: delayed import — only needed for this check

        for manifest in sorted(agents_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(manifest.read_text()) or {}
                inst_file = data.get("instruction_file")
                if inst_file and not (repo_root / inst_file).exists():
                    improvements.append({
                        "title": f"Fix broken instruction_file in {manifest.name}",
                        "description": (
                            f"Agent manifest `docs/agents/{manifest.name}` references "
                            f"instruction_file `{inst_file}` which does not exist. "
                            f"Either create the missing file or update the manifest "
                            f"to point to the correct path."
                        ),
                        "category": "bugfix",
                        "priority": "P1",
                    })
            except Exception:
                continue

    return improvements
