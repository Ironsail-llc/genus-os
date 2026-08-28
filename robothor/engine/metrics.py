"""Prometheus metrics for the Agent Engine.

Provides pre-defined counters, histograms, and gauges for agent runs, LLM calls,
tool usage, and connection pool health. Instrumentation points call these from
runner.py, tools, and db/connection.py.

The ``/metrics`` endpoint is registered in health.py.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# ── Agent Runs ──────────────────────────────────────────────────────────

AGENT_RUNS_TOTAL = Counter(
    "robothor_agent_runs_total",
    "Total agent runs",
    ["agent_id", "status"],
)

AGENT_RUN_DURATION = Histogram(
    "robothor_agent_run_duration_seconds",
    "Agent run duration in seconds",
    ["agent_id"],
    buckets=[5, 15, 30, 60, 120, 300, 600, 1200, 3600],
)

ACTIVE_AGENTS = Gauge(
    "robothor_active_agents",
    "Number of agents currently running",
)

# ── Execution mode ─────────────────────────────────────────────────────

#: Which economics are in force. A gauge per mode rather than one numeric code,
#: so a dashboard can graph "time spent local" without decoding an enum.
EXECUTION_MODE = Gauge(
    "robothor_execution_mode",
    "1 for the execution mode currently in force, 0 for the others",
    ["mode"],
)

#: Work the admission gate held back. Labelled by mode so a SHADOW deferral in
#: observe is countable separately from a real one in enforce -- that
#: distinction is the evidence a promotion decision rests on.
ADMISSION_DEFERRALS_TOTAL = Counter(
    "robothor_admission_deferrals_total",
    "Runs deferred (or, in observe, that would have been) by admission control",
    ["mode", "priority"],
)

_EXECUTION_MODES = ("cloud", "local")


def set_execution_mode(mode: str) -> None:
    """Record the mode in force, clearing the others.

    Both modes reading 1 would make every dashboard built on this lie, so this
    always writes all of them. Never raises: telemetry must not be able to
    break the thing it observes.
    """
    try:
        for known in _EXECUTION_MODES:
            EXECUTION_MODE.labels(mode=known).set(1 if known == mode else 0)
    except Exception:  # pragma: no cover - metrics are never load-bearing
        logger.debug("Could not set execution mode gauge", exc_info=True)


def record_admission_deferral(mode: str | None, priority: str | None) -> None:
    """Count one deferral. Never raises."""
    try:
        ADMISSION_DEFERRALS_TOTAL.labels(
            mode=str(mode or "unknown"), priority=str(priority or "unknown")
        ).inc()
    except Exception:  # pragma: no cover - metrics are never load-bearing
        logger.debug("Could not record admission deferral", exc_info=True)


# ── LLM Calls ──────────────────────────────────────────────────────────

LLM_CALLS_TOTAL = Counter(
    "robothor_llm_calls_total",
    "Total LLM API calls",
    ["model", "status"],
)

LLM_CALL_DURATION = Histogram(
    "robothor_llm_call_duration_seconds",
    "LLM call duration in seconds",
    ["model"],
    buckets=[1, 2, 5, 10, 30, 60, 120],
)

LLM_TOKENS_TOTAL = Counter(
    "robothor_llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "direction"],  # direction: input, output
)

# ── Tool Calls ──────────────────────────────────────────────────────────

TOOL_CALLS_TOTAL = Counter(
    "robothor_tool_calls_total",
    "Total tool invocations",
    ["tool_name", "status"],
)

TOOL_CALL_DURATION = Histogram(
    "robothor_tool_call_duration_seconds",
    "Tool call duration in seconds",
    ["tool_name"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)

# ── Database Pool ───────────────────────────────────────────────────────

DB_POOL_CONNECTIONS = Gauge(
    "robothor_db_pool_connections",
    "Database connection pool size",
    ["state"],  # state: used, free
)

# ── Adapter ─────────────────────────────────────────────────────────────

ADAPTER_FAILURES = Counter(
    "robothor_adapter_failures_total",
    "Adapter connection failures",
    ["adapter_name"],
)

# ── Thread Planner ──────────────────────────────────────────────────────

PLANNER_ACTIONS_TOTAL = Counter(
    "robothor_planner_actions_total",
    # Incremented at action dispatch in apply_plan, before the DAL write.
    # An execute with missing next_action, or set_next_action returning
    # False, still increments — so the counter measures attempted decisions
    # ("the planner chose this action this beat"), NOT successful writes.
    # Dashboards keying off this for write-success rates need to also pull
    # task.created / task.updated events.
    "Forward-planner actions attempted per beat (dispatch attempts, not successful writes)",
    ["action", "tenant"],  # action: execute | ask | wait | close
)

PLANNER_RUN_DURATION = Histogram(
    "robothor_planner_run_duration_seconds",
    "Wall-clock time per plan_all_stalled invocation",
    ["tenant"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
)
