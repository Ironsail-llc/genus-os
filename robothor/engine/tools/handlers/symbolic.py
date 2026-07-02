"""Symbolic-memory tool handlers — recall_node (Rip 13).

``recall_node(node_id)`` returns the byte-exact full output of a prior tool
step that was condensed into the run's task-state graph. Gated by
``ROBOTHOR_RIP_13_ENABLED`` (via symbolic_memory_mode).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine.feature_flags import symbolic_memory_mode

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("recall_node")
async def _recall_node(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if symbolic_memory_mode() == "off":
        return {"error": "symbolic memory disabled (set ROBOTHOR_RIP_13_ENABLED=1)"}
    from robothor.engine.symbolic_memory import get_graph

    node_id = args.get("node_id", "")
    run_id = str(getattr(ctx, "run_id", "") or "")
    graph = get_graph(run_id)
    if graph is None:
        return {"error": "no symbol graph for this run", "node_id": node_id}

    ref = graph.get_ref(node_id)
    if not ref:
        return {"error": "node not found or has no stored output", "node_id": node_id}
    try:
        content = Path(ref).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"could not read node output: {e}", "node_id": node_id}
    return {"node_id": node_id, "content": content}
