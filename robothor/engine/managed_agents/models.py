"""Standalone dataclasses for the Managed Agents integration.

These are independent of ``robothor.engine.models`` — no existing
dataclasses are imported or modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MAAgentConfig:
    """Configuration for creating an agent on Managed Agents."""

    name: str
    model: str  # e.g. "claude-sonnet-4-6"
    system_prompt: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    callable_agents: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MAEnvironmentConfig:
    """Configuration for creating an MA environment."""

    name: str
    networking: str = "unrestricted"  # "unrestricted" or "restricted"


@dataclass
class MASessionConfig:
    """Configuration for creating an MA session."""

    agent_id: str  # MA agent ID (returned by create_agent)
    agent_version: int = 1
    environment_id: str = ""  # MA environment ID
    resources: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""


@dataclass
class MARunResult:
    """Result from a Managed Agents session.

    This is standalone — not related to ``AgentRun``.
    """

    session_id: str = ""
    output_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    outcome_result: str | None = None  # "satisfied" | "needs_revision" | ...
    outcome_explanation: str | None = None
    duration_ms: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
