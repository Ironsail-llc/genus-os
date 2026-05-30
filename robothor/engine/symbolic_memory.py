"""Symbolic short-term memory — dense task-state graph over tool logs (Rip 13).

Ripped from Tencent/TencentDB-Agent-Memory: instead of carrying verbose tool
output (or even truncated summaries) in the context window, encode the run's
tool activity as a compact Mermaid flowchart keyed by ``node_id``. The agent
reasons over the symbol graph and, when it needs the full output of a step,
calls ``recall_node(node_id)`` to read the byte-exact original from disk.

This builds on the existing offload in ``session._offload_tool_result`` (which
already writes large tool outputs to a tempfile). Here we additionally:
    * give each offloaded step a stable ``node_id`` and a graph node, and
    * expose the full output by ``node_id`` via a per-run registry.

Modes (``feature_flags.symbolic_memory_mode``):
    observe  — build the graph + log would-be token savings; context unchanged.
    enforce  — the runner injects ``render_injection_block`` instead of raw
               tool tails (wired once the runner refactor lands).

Ratio knobs (env, ported from the source project):
    ROBOTHOR_MEMORY_OFFLOAD_MILD_RATIO        (default 0.5)
    ROBOTHOR_MEMORY_OFFLOAD_AGGRESSIVE_RATIO  (default 0.85)
    ROBOTHOR_MEMORY_MMD_MAX_TOKEN_RATIO       (default 0.2)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Module-level per-run registry so the recall_node tool can find a run's graph
# from a ToolContext without threading the session through the tool layer.
_REGISTRY: dict[str, SymbolGraph] = {}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def mild_ratio() -> float:
    return _env_float("ROBOTHOR_MEMORY_OFFLOAD_MILD_RATIO", 0.5)


def aggressive_ratio() -> float:
    return _env_float("ROBOTHOR_MEMORY_OFFLOAD_AGGRESSIVE_RATIO", 0.85)


def mmd_max_token_ratio() -> float:
    return _env_float("ROBOTHOR_MEMORY_MMD_MAX_TOKEN_RATIO", 0.2)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — good enough for ratio gating."""
    return max(1, len(text) // 4)


@dataclass
class SymbolNode:
    node_id: str
    tool_name: str
    summary: str
    ref_path: str | None = None  # full output on disk, if offloaded
    full_chars: int = 0  # size of the original output (for savings accounting)


@dataclass
class SymbolGraph:
    """Per-run task-state graph of tool calls."""

    run_id: str
    nodes: list[SymbolNode] = field(default_factory=list)

    def add_node(
        self,
        tool_name: str,
        summary: str,
        *,
        ref_path: str | None = None,
        full_chars: int = 0,
    ) -> str:
        node_id = f"n{len(self.nodes) + 1}"
        # Mermaid node labels can't contain unescaped quotes/newlines/brackets.
        clean = summary.replace('"', "'").replace("\n", " ").strip()
        if len(clean) > 80:
            clean = clean[:77] + "..."
        self.nodes.append(
            SymbolNode(
                node_id=node_id,
                tool_name=tool_name,
                summary=clean,
                ref_path=ref_path,
                full_chars=full_chars,
            )
        )
        return node_id

    def get_ref(self, node_id: str) -> str | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n.ref_path
        return None

    def to_mermaid(self) -> str:
        """Render the graph as a Mermaid flowchart (nodes + sequential edges)."""
        if not self.nodes:
            return ""
        lines = ["flowchart TD"]
        for n in self.nodes:
            ref = "  ⎘" if n.ref_path else ""
            lines.append(f'    {n.node_id}["{n.tool_name}: {n.summary}{ref}"]')
        for a, b in zip(self.nodes, self.nodes[1:], strict=False):
            lines.append(f"    {a.node_id} --> {b.node_id}")
        return "\n".join(lines)

    def render_injection_block(self) -> str:
        """The compact block the runner injects in enforce mode."""
        if not self.nodes:
            return ""
        return (
            "# Task-state graph (symbolic memory)\n"
            "Each node is a prior tool step. To read a step's full output, call "
            "recall_node(node_id).\n\n```mermaid\n"
            f"{self.to_mermaid()}\n```"
        )

    def savings(self) -> dict[str, int]:
        """Token accounting: raw output tokens vs the symbol-graph tokens."""
        raw_tokens = sum(estimate_tokens("x" * n.full_chars) for n in self.nodes)
        graph_tokens = estimate_tokens(self.render_injection_block())
        return {
            "raw_tokens": raw_tokens,
            "graph_tokens": graph_tokens,
            "saved_tokens": max(0, raw_tokens - graph_tokens),
            "nodes": len(self.nodes),
        }


def get_or_create_graph(run_id: str) -> SymbolGraph:
    g = _REGISTRY.get(run_id)
    if g is None:
        g = SymbolGraph(run_id=run_id)
        _REGISTRY[run_id] = g
    return g


def get_graph(run_id: str) -> SymbolGraph | None:
    return _REGISTRY.get(run_id)


def clear_graph(run_id: str) -> None:
    _REGISTRY.pop(run_id, None)
