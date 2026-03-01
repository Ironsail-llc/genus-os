"""Deep Reason tool — RLM integration for heavy-context reasoning.

Wraps the `rlms` library to give agents programmatic access to Robothor's
full memory system via a Python REPL.  The RLM session can write code that
calls search_memory, get_entity, read_file, and memory_block_read from
inside the REPL, navigating large context far more effectively than
vanilla LLM prompts.

Called from ``tools.py`` via ``asyncio.to_thread()`` (sync dispatch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─── Configuration ───────────────────────────────────────────────────


@dataclass
class DeepReasonConfig:
    """Runtime configuration for a deep_reason call."""

    root_model: str = field(
        default_factory=lambda: os.environ.get(
            "ROBOTHOR_RLM_ROOT_MODEL", "openrouter/anthropic/claude-sonnet-4-6"
        )
    )
    sub_model: str = field(
        default_factory=lambda: os.environ.get(
            "ROBOTHOR_RLM_SUB_MODEL", "openrouter/anthropic/claude-haiku-4-5-20251001"
        )
    )
    max_budget: float = field(
        default_factory=lambda: float(os.environ.get("ROBOTHOR_RLM_MAX_BUDGET", "2.0"))
    )
    max_timeout: int = field(
        default_factory=lambda: int(os.environ.get("ROBOTHOR_RLM_MAX_TIMEOUT", "240"))
    )
    max_iterations: int = field(
        default_factory=lambda: int(os.environ.get("ROBOTHOR_RLM_MAX_ITERATIONS", "30"))
    )
    max_depth: int = field(
        default_factory=lambda: int(os.environ.get("ROBOTHOR_RLM_MAX_DEPTH", "1"))
    )
    workspace: str = ""
    log_dir: str = field(
        default_factory=lambda: os.environ.get(
            "ROBOTHOR_RLM_LOG_DIR",
            os.path.expanduser("~/clawd/memory/rlm-traces"),
        )
    )


# ─── Sync wrappers for async memory tools ────────────────────────────
# RLM REPL runs sync Python.  These wrappers use asyncio.run() to bridge
# from sync → async since deep_reason itself runs in a separate OS thread
# (via asyncio.to_thread) with no event loop.


def _make_search_memory_fn():
    """Return a sync wrapper around ``search_facts``."""

    def search_memory(query: str, limit: int = 10) -> str:
        """Search Robothor's semantic memory. Returns JSON list of facts."""
        from robothor.memory.facts import search_facts

        results = asyncio.run(search_facts(query, limit=limit))
        return json.dumps(
            [
                {
                    "fact": r["fact_text"],
                    "category": r["category"],
                    "confidence": r["confidence"],
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in results
            ],
            indent=2,
        )

    return search_memory


def _make_get_entity_fn():
    """Return a sync wrapper around ``get_entity``."""

    def get_entity(name: str) -> str:
        """Look up an entity and its relationships in the knowledge graph. Returns JSON."""
        from robothor.memory.entities import get_entity as _get_entity

        result = asyncio.run(_get_entity(name))
        return json.dumps(result or {"name": name, "found": False}, indent=2)

    return get_entity


def _make_read_file_fn(workspace: str):
    """Return a sync file reader with 50 KB truncation."""

    def read_file(path: str) -> str:
        """Read a file from the workspace. Truncated to 50 KB."""
        p = Path(path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        try:
            text = p.read_text()
            if len(text) > 50_000:
                text = (
                    text[:50_000] + f"\n\n... [truncated at 50KB, full file is {len(text)} chars]"
                )
            return text
        except Exception as e:
            return f"Error reading {path}: {e}"

    return read_file


def _make_memory_block_read_fn():
    """Return a sync wrapper around ``read_block``."""

    def memory_block_read(block_name: str) -> str:
        """Read a named memory block. Returns JSON."""
        from robothor.memory.blocks import read_block

        result = read_block(block_name)
        return json.dumps(result, indent=2)

    return memory_block_read


# ─── Context pre-loading ─────────────────────────────────────────────


def _load_context_source(src: dict[str, Any], workspace: str) -> str | None:
    """Pre-load a single context source. Returns formatted string or None."""
    src_type = src.get("type", "")

    if src_type == "memory":
        query = src.get("query", "")
        limit = src.get("limit", 10)
        if not query:
            return None
        from robothor.memory.facts import search_facts

        results = asyncio.run(search_facts(query, limit=limit))
        if not results:
            return None
        lines = [f"## Memory search: {query}"]
        for r in results:
            lines.append(f"- [{r['category']}, conf={r['confidence']}] {r['fact_text']}")
        return "\n".join(lines)

    if src_type == "file":
        path = src.get("path", "")
        if not path:
            return None
        p = Path(path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        try:
            text = p.read_text()
            if len(text) > 50_000:
                text = text[:50_000] + "\n... [truncated at 50KB]"
            return f"## File: {path}\n\n{text}"
        except Exception as e:
            logger.warning("Failed to load file context %s: %s", path, e)
            return None

    if src_type == "block":
        block_name = src.get("block_name", "")
        if not block_name:
            return None
        from robothor.memory.blocks import read_block

        result = read_block(block_name)
        if "error" in result:
            return None
        return f"## Memory block: {block_name}\n\n{result.get('content', '')}"

    if src_type == "entity":
        name = src.get("name", "")
        if not name:
            return None
        from robothor.memory.entities import get_entity

        entity: dict[str, Any] | None = asyncio.run(get_entity(name))  # type: ignore[arg-type]
        if not entity:
            return None
        return f"## Entity: {name}\n\n{json.dumps(entity, indent=2)}"

    logger.warning("Unknown context source type: %s", src_type)
    return None


# ─── Custom tools for the RLM REPL ──────────────────────────────────


def _build_custom_tools(workspace: str) -> dict[str, dict[str, Any]]:
    """Build the custom_tools dict for the RLM instance."""
    return {
        "search_memory": {
            "tool": _make_search_memory_fn(),
            "description": (
                "Search Robothor's semantic memory for facts matching a query. "
                "Returns JSON list of facts with category, confidence, similarity."
            ),
        },
        "get_entity": {
            "tool": _make_get_entity_fn(),
            "description": (
                "Look up an entity (person, org, concept) and its relationships "
                "in the knowledge graph. Returns JSON."
            ),
        },
        "read_file": {
            "tool": _make_read_file_fn(workspace),
            "description": (
                "Read a file from the workspace. Paths relative to workspace root. "
                "Truncated to 50KB."
            ),
        },
        "memory_block_read": {
            "tool": _make_memory_block_read_fn(),
            "description": (
                "Read a named memory block (persistent structured working memory). "
                "Returns JSON with block_name, content, last_written_at."
            ),
        },
    }


# ─── Main entry point ────────────────────────────────────────────────


def execute_deep_reason(
    query: str,
    context: str = "",
    context_sources: list[dict[str, Any]] | None = None,
    config: DeepReasonConfig | None = None,
) -> dict[str, Any]:
    """Run a deep reasoning session using the RLM library.

    This is a sync function — called via ``asyncio.to_thread()`` from the
    tool executor in ``tools.py``.

    Returns a dict with ``response``, ``execution_time_s``, ``cost_usd``,
    ``context_chars``, and ``trajectory_file``, or an ``error`` key on failure.
    """
    if config is None:
        config = DeepReasonConfig()

    workspace = config.workspace

    # 1. Pre-load context from sources
    context_parts: list[str] = []
    if context:
        context_parts.append(context)

    if context_sources:
        for src in context_sources:
            try:
                loaded = _load_context_source(src, workspace)
                if loaded:
                    context_parts.append(loaded)
            except Exception as e:
                logger.warning("Failed to load context source %s: %s", src, e)

    full_context = "\n\n---\n\n".join(context_parts) if context_parts else ""

    # 2. Build custom tools
    custom_tools = _build_custom_tools(workspace)

    # 3. Create log directory
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 4. Initialize RLM and run
    start_time = time.monotonic()
    try:
        from rlm import RLM
        from rlm.logger import RLMLogger

        rlm_logger = RLMLogger(log_dir=str(log_dir))

        rlm = RLM(
            backend="litellm",
            backend_kwargs={"model_name": config.root_model},
            other_backends=["litellm"],
            other_backend_kwargs=[{"model_name": config.sub_model}],
            environment="local",
            max_budget=config.max_budget,
            max_timeout=config.max_timeout,
            max_iterations=config.max_iterations,
            max_depth=config.max_depth,
            custom_tools=custom_tools,
            compaction=True,
            logger=rlm_logger,
        )

        result = rlm.completion({"context": full_context, "query": query})

        elapsed = time.monotonic() - start_time

        # Extract cost from usage summary
        cost = 0.0
        if result.usage_summary and result.usage_summary.total_cost:
            cost = result.usage_summary.total_cost

        # Extract trajectory file path from logger
        trajectory_file = getattr(rlm_logger, "log_file_path", None)

        return {
            "response": result.response,
            "execution_time_s": round(elapsed, 1),
            "cost_usd": round(cost, 4),
            "context_chars": len(full_context),
            "trajectory_file": trajectory_file,
        }

    except ImportError:
        return {"error": ("rlms package not installed. Install with: pip install rlms")}
    except Exception as e:
        elapsed = time.monotonic() - start_time
        error_type = type(e).__name__
        # Handle known RLM exceptions by type name (avoids importing them)
        if error_type == "BudgetExceededError":
            return {
                "error": f"RLM budget exceeded (${config.max_budget} limit): {e}",
                "execution_time_s": round(elapsed, 1),
                "partial": True,
            }
        if error_type == "TimeoutExceededError":
            return {
                "error": f"RLM timeout exceeded ({config.max_timeout}s limit): {e}",
                "execution_time_s": round(elapsed, 1),
                "partial": True,
            }
        logger.error("deep_reason failed: %s", e, exc_info=True)
        return {"error": f"deep_reason failed: {error_type}: {e}"}
