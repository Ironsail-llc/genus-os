"""
Self-Model — Dynamic Capability Self-Assessment for Genus OS.

Gathers fleet health, memory stats, and agent learnings, then uses
LLM synthesis to produce a structured self-assessment. Written to
the `self_model` memory block for consumption by Agent Architect
and AutoAgent.

Runs as Phase 7 in the nightly intelligence pipeline (Tier 3).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from robothor.engine.analytics import (
    detect_anomalies,
    get_agent_stats,
    get_failure_patterns,
    get_fleet_health,
)
from robothor.llm import ollama as llm_client
from robothor.memory.blocks import read_block, write_block
from robothor.memory.facts import get_memory_stats

logger = logging.getLogger(__name__)

# Top N agents by run volume to collect detailed stats for.
_TOP_AGENTS = 8


async def build_self_model(days: int = 7) -> dict[str, Any]:
    """Gather fleet health, memory stats, and learnings into a self-assessment.

    Args:
        days: Lookback window for analytics data.

    Returns:
        Dict with model_text (LLM-synthesized markdown), generated_at timestamp,
        and raw data (fleet_health, memory_stats, anomalies).
    """
    generated_at = datetime.now(UTC).isoformat()

    # ── Gather data (graceful on failures) ────────────────────────────────
    fleet_health: dict[str, Any] = {}
    try:
        fleet_health = get_fleet_health(days=days)
    except Exception:
        logger.warning("Self-model: fleet health query failed", exc_info=True)

    # Get per-agent stats for top agents by volume
    per_agent_stats: dict[str, Any] = {}
    agents_list = fleet_health.get("agents", [])
    top_agents = sorted(agents_list, key=lambda a: a.get("total_runs", 0), reverse=True)[
        :_TOP_AGENTS
    ]
    for agent in top_agents:
        aid = agent.get("agent_id", "")
        try:
            per_agent_stats[aid] = get_agent_stats(aid, days=days)
        except Exception:
            logger.debug("Self-model: stats failed for %s", aid)

    # Detect anomalies for top agents
    anomalies: list[dict[str, Any]] = []
    for agent in top_agents:
        aid = agent.get("agent_id", "")
        try:
            result = detect_anomalies(aid)
            if result.get("anomalies"):
                anomalies.append(result)
        except Exception:
            pass

    # Failure patterns
    failure_patterns: dict[str, Any] = {}
    try:
        failure_patterns = get_failure_patterns(hours=days * 24)
    except Exception:
        logger.debug("Self-model: failure patterns query failed")

    # Memory stats
    memory_stats: dict[str, Any] = {}
    try:
        memory_stats = get_memory_stats()
    except Exception:
        logger.debug("Self-model: memory stats query failed")

    # Read learnings blocks
    learnings: dict[str, str] = {}
    for block_name in (
        "autoagent_learnings",
        "autoresearch_learnings",
        "curiosity_engine_findings",
    ):
        try:
            block = read_block(block_name)
            content = block.get("content", "")
            if content:
                # Truncate to last 1000 chars to keep LLM context reasonable
                learnings[block_name] = content[-1000:]
        except Exception:
            pass

    # ── LLM synthesis ─────────────────────────────────────────────────────
    data_summary = _format_data_for_synthesis(
        fleet_health, per_agent_stats, anomalies, failure_patterns, memory_stats, learnings
    )

    model_text = ""
    try:
        model_text = await llm_client.generate(
            prompt=data_summary,
            system=(
                "You are analyzing a multi-agent AI system's performance data. "
                "Produce a structured self-assessment in markdown with these exact sections:\n"
                "## Strengths\n## Weaknesses\n## Coverage Gaps\n"
                "## Improvement Trajectory\n## Recommended Priorities\n\n"
                "Be specific and data-driven. Each item should name an agent or metric. "
                "Recommended Priorities should be numbered and actionable."
            ),
            max_tokens=2048,
        )
    except Exception:
        logger.warning("Self-model: LLM synthesis failed", exc_info=True)
        model_text = _fallback_model_text(fleet_health, memory_stats)

    return {
        "model_text": model_text,
        "generated_at": generated_at,
        "fleet_health": fleet_health,
        "memory_stats": memory_stats,
        "anomalies": anomalies,
        "per_agent_stats": per_agent_stats,
    }


async def write_self_model(model: dict[str, Any]) -> None:
    """Format and persist the self-model to a memory block.

    Args:
        model: Dict from build_self_model().
    """
    try:
        content = f"# System Self-Model — {model.get('generated_at', 'unknown')}\n\n"
        content += model.get("model_text", "No model generated.")
        write_block("self_model", content)
        logger.info("Self-model written to memory block")
    except Exception:
        logger.warning("Failed to write self-model block", exc_info=True)


def _format_data_for_synthesis(
    fleet_health: dict[str, Any],
    per_agent_stats: dict[str, Any],
    anomalies: list[dict[str, Any]],
    failure_patterns: dict[str, Any],
    memory_stats: dict[str, Any],
    learnings: dict[str, str],
) -> str:
    """Format raw data into a structured prompt for LLM synthesis."""
    lines = ["Analyze this system data and produce a self-assessment:\n"]

    # Fleet health summary
    totals = fleet_health.get("fleet_totals", {})
    if totals:
        lines.append(
            f"Fleet totals (7d): {totals.get('total_runs', 0)} runs, "
            f"{totals.get('success_rate', 0):.1%} success rate, "
            f"${totals.get('total_cost_usd', 0):.2f} total cost"
        )

    # Per-agent stats
    for aid, stats in per_agent_stats.items():
        lines.append(
            f"  {aid}: {stats.get('total_runs', 0)} runs, "
            f"{stats.get('success_rate', 0):.1%} success, "
            f"${stats.get('avg_cost_usd', 0):.3f}/run avg, "
            f"errors: {stats.get('top_error_types', [])}"
        )

    # Anomalies
    if anomalies:
        lines.append("\nAnomalies detected:")
        for a in anomalies:
            lines.extend(
                f"  {a.get('agent_id', '?')}: {anomaly.get('metric', '?')} "
                f"deviated {anomaly.get('sigma_deviation', 0):.1f} sigma "
                f"({anomaly.get('direction', '?')})"
                for anomaly in a.get("anomalies", [])
            )

    # Failure patterns
    patterns = failure_patterns.get("patterns", [])
    if patterns:
        lines.append("\nFailure patterns:")
        lines.extend(f"  {p}" for p in patterns[:5])

    # Memory stats
    if memory_stats:
        lines.append(
            f"\nMemory: {memory_stats.get('total_facts', 0)} total facts "
            f"({memory_stats.get('active_facts', 0)} active), "
            f"{memory_stats.get('entity_count', 0)} entities, "
            f"{memory_stats.get('relation_count', 0)} relations"
        )

    # Recent learnings
    for block_name, content in learnings.items():
        lines.append(f"\nRecent {block_name}:\n{content[:500]}")

    return "\n".join(lines)


def _fallback_model_text(fleet_health: dict[str, Any], memory_stats: dict[str, Any]) -> str:
    """Generate a basic model text when LLM synthesis fails."""
    totals = fleet_health.get("fleet_totals", {})
    return (
        "## Strengths\n"
        f"- Fleet success rate: {totals.get('success_rate', 0):.1%}\n\n"
        "## Weaknesses\n"
        "- LLM synthesis unavailable for detailed analysis\n\n"
        "## Coverage Gaps\n"
        f"- {memory_stats.get('entity_count', 0)} entities, "
        f"{memory_stats.get('relation_count', 0)} relations\n\n"
        "## Improvement Trajectory\n"
        "- Data insufficient for trend analysis\n\n"
        "## Recommended Priorities\n"
        "1. Investigate LLM availability for self-model synthesis\n"
    )
