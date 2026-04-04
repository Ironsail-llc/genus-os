"""Prometheus metrics for the Agent Engine.

Provides pre-defined counters, histograms, and gauges for agent runs, LLM calls,
tool usage, and connection pool health. Instrumentation points call these from
runner.py, tools, and db/connection.py.

The ``/metrics`` endpoint is registered in health.py.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
