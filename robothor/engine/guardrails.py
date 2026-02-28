"""
Guardrails Framework — policy enforcement for tool calls.

Runs pre-execution checks on tool calls and post-execution checks on results.
Named policies are registered globally and enabled per-agent via YAML manifest.
All events are logged to the agent_guardrail_events table.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Patterns for destructive commands
DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
    re.compile(r"\brm\s+-r\s+/", re.IGNORECASE),
]

# Patterns for sensitive data in output
SENSITIVE_PATTERNS = [
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),  # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI-style API key
    re.compile(r"ghp_[a-zA-Z0-9]{30,}"),  # GitHub PAT
    re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+"),  # Slack bot token
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),  # Private key
]

# Default rate limit
DEFAULT_RATE_LIMIT = 30  # per minute


@dataclass
class GuardrailResult:
    """Result of a guardrail check."""

    allowed: bool = True
    action: str = "allowed"  # allowed, blocked, warned
    reason: str = ""
    guardrail_name: str = ""


@dataclass
class GuardrailEngine:
    """Runs pre/post execution checks based on enabled policies."""

    enabled_policies: list[str] = field(default_factory=list)
    _rate_counts: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def check_pre_execution(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_id: str = "",
    ) -> GuardrailResult:
        """Run all enabled pre-execution guardrails on a tool call."""
        for policy in self.enabled_policies:
            result = self._run_pre_policy(policy, tool_name, tool_args, agent_id)
            if not result.allowed:
                return result
        return GuardrailResult()

    def check_post_execution(
        self,
        tool_name: str,
        tool_output: Any,
    ) -> GuardrailResult:
        """Run all enabled post-execution guardrails on tool output."""
        for policy in self.enabled_policies:
            result = self._run_post_policy(policy, tool_name, tool_output)
            if result.action == "warned":
                return result
        return GuardrailResult()

    def _run_pre_policy(
        self,
        policy: str,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_id: str,
    ) -> GuardrailResult:
        """Dispatch to the correct pre-execution policy."""
        if policy == "no_destructive_writes":
            return self._check_destructive(tool_name, tool_args)
        if policy == "no_external_http":
            return self._check_external_http(tool_name)
        if policy == "rate_limit":
            return self._check_rate_limit(agent_id)
        return GuardrailResult()

    def _run_post_policy(
        self,
        policy: str,
        tool_name: str,
        tool_output: Any,
    ) -> GuardrailResult:
        """Dispatch to the correct post-execution policy."""
        if policy == "no_sensitive_data":
            return self._check_sensitive_output(tool_name, tool_output)
        return GuardrailResult()

    def _check_destructive(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> GuardrailResult:
        """Block destructive commands in exec/shell tools."""
        if tool_name not in ("exec", "shell"):
            return GuardrailResult()

        command = str(tool_args.get("command", ""))
        for pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return GuardrailResult(
                    allowed=False,
                    action="blocked",
                    reason=f"Destructive command blocked: {pattern.pattern}",
                    guardrail_name="no_destructive_writes",
                )
        return GuardrailResult()

    def _check_external_http(self, tool_name: str) -> GuardrailResult:
        """Block web_fetch/web_search for isolated agents."""
        if tool_name in ("web_fetch", "web_search"):
            return GuardrailResult(
                allowed=False,
                action="blocked",
                reason=f"External HTTP blocked for this agent: {tool_name}",
                guardrail_name="no_external_http",
            )
        return GuardrailResult()

    def _check_rate_limit(self, agent_id: str) -> GuardrailResult:
        """Rate limit: max N tool calls per minute."""
        now = time.monotonic()
        key = agent_id or "_default"
        calls = self._rate_counts[key]

        # Prune calls older than 60s
        cutoff = now - 60
        self._rate_counts[key] = [t for t in calls if t > cutoff]
        calls = self._rate_counts[key]

        if len(calls) >= DEFAULT_RATE_LIMIT:
            return GuardrailResult(
                allowed=False,
                action="blocked",
                reason=f"Rate limit exceeded: {len(calls)}/{DEFAULT_RATE_LIMIT} calls/min",
                guardrail_name="rate_limit",
            )
        calls.append(now)
        return GuardrailResult()

    def _check_sensitive_output(
        self, tool_name: str, tool_output: Any
    ) -> GuardrailResult:
        """Warn if tool output contains sensitive data patterns."""
        output_str = str(tool_output)[:10000]  # cap scan length
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(output_str):
                return GuardrailResult(
                    allowed=True,
                    action="warned",
                    reason=f"Possible sensitive data in {tool_name} output: {pattern.pattern}",
                    guardrail_name="no_sensitive_data",
                )
        return GuardrailResult()
