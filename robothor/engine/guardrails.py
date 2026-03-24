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

# Default guardrails applied to all agents unless opted out
DEFAULT_GUARDRAILS = ["no_destructive_writes", "no_sensitive_data", "rate_limit"]


def compute_effective_guardrails(
    configured: list[str],
    opt_out: bool = False,
) -> list[str]:
    """Compute effective guardrail list by merging defaults with agent config.

    If opt_out is True, only use explicitly configured guardrails.
    Otherwise, merge DEFAULT_GUARDRAILS with configured (deduplicated).
    """
    if opt_out:
        return configured

    # Merge: defaults + agent-specific, deduplicated, preserving order
    seen: set[str] = set()
    result: list[str] = []
    for policy in DEFAULT_GUARDRAILS + configured:
        if policy not in seen:
            seen.add(policy)
            result.append(policy)
    return result


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
    workspace: str = ""  # Workspace root for normalizing absolute paths
    _exec_allowlists: dict[str, list[re.Pattern]] = field(default_factory=dict)  # type: ignore[type-arg]
    _write_allowlists: dict[str, list[str]] = field(default_factory=dict)
    _rate_counts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

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
        if policy == "no_main_branch_push":
            return self._check_no_main_branch(tool_name, tool_args)
        if policy == "rate_limit":
            return self._check_rate_limit(agent_id)
        if policy == "exec_allowlist":
            return self._check_exec_allowlist(tool_name, tool_args, agent_id)
        if policy == "write_path_restrict":
            return self._check_write_path(tool_name, tool_args, agent_id)
        if policy == "desktop_safety":
            return self._check_desktop_safety(tool_name, tool_args)
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

    def _check_destructive(self, tool_name: str, tool_args: dict[str, Any]) -> GuardrailResult:
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

    def _check_no_main_branch(self, tool_name: str, tool_args: dict[str, Any]) -> GuardrailResult:
        """Block git operations targeting main/master branches."""
        protected = {"main", "master"}

        if tool_name == "git_branch":
            branch = tool_args.get("branch_name", "")
            if branch in protected:
                return GuardrailResult(
                    allowed=False,
                    action="blocked",
                    reason=f"Cannot create/switch to protected branch: {branch}",
                    guardrail_name="no_main_branch_push",
                )

        if tool_name == "git_push":
            # The tool itself checks the current branch, but this guardrail provides
            # a belt-and-suspenders pre-execution check
            return GuardrailResult()  # Allowed — tool enforces at runtime

        if tool_name == "git_commit":
            # git_commit also checks branch at runtime, guardrail is advisory here
            return GuardrailResult()

        # Block any exec command that looks like git push to main/master
        if tool_name in ("exec", "shell"):
            command = str(tool_args.get("command", ""))
            for branch in protected:
                if re.search(rf"\bgit\s+push\b.*\b{branch}\b", command):
                    return GuardrailResult(
                        allowed=False,
                        action="blocked",
                        reason=f"Cannot push to protected branch via exec: {branch}",
                        guardrail_name="no_main_branch_push",
                    )

        return GuardrailResult()

    def _check_exec_allowlist(
        self, tool_name: str, tool_args: dict[str, Any], agent_id: str
    ) -> GuardrailResult:
        """Block exec/shell commands not matching the agent's allowlist patterns."""
        if tool_name not in ("exec", "shell"):
            return GuardrailResult()
        patterns = self._exec_allowlists.get(agent_id, [])
        if not patterns:  # No allowlist configured = no restriction (backward compat)
            return GuardrailResult()
        command = str(tool_args.get("command", ""))
        for pattern in patterns:
            if pattern.search(command):
                return GuardrailResult()
        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=f"exec command not in allowlist: {command[:100]}",
            guardrail_name="exec_allowlist",
        )

    def _check_write_path(
        self, tool_name: str, tool_args: dict[str, Any], agent_id: str
    ) -> GuardrailResult:
        """Block write_file to paths not matching the agent's allowlist globs."""
        if tool_name != "write_file":
            return GuardrailResult()
        patterns = self._write_allowlists.get(agent_id, [])
        if not patterns:  # No allowlist = no restriction
            return GuardrailResult()
        path = str(tool_args.get("path", ""))
        # Normalize absolute paths to workspace-relative for matching
        ws = self.workspace.rstrip("/") + "/" if self.workspace else ""
        if ws and path.startswith(ws):
            path = path[len(ws) :]
        from fnmatch import fnmatch

        for pattern in patterns:
            if fnmatch(path, pattern):
                return GuardrailResult()
        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=f"write_file path not allowed: {path}",
            guardrail_name="write_path_restrict",
        )

    def _check_desktop_safety(self, tool_name: str, tool_args: dict[str, Any]) -> GuardrailResult:
        """Safety guardrails for desktop control and browser tools."""
        # Block launching terminal emulators (use exec tool instead)
        if tool_name == "desktop_launch":
            app = str(tool_args.get("app", "")).lower()
            blocked_apps = {
                "bash",
                "sh",
                "zsh",
                "fish",
                "xterm",
                "gnome-terminal",
                "konsole",
                "alacritty",
                "kitty",
                "terminal",
                "xfce4-terminal",
            }
            app_base = app.rsplit("/", 1)[-1]
            if app_base in blocked_apps:
                return GuardrailResult(
                    allowed=False,
                    action="blocked",
                    reason=f"Cannot launch terminal emulator '{app}' — use the exec tool for shell commands",
                    guardrail_name="desktop_safety",
                )

        # Block dangerous key combinations
        if tool_name == "desktop_key":
            combo = str(tool_args.get("key", "")).lower().replace(" ", "")
            dangerous_combos = {
                "ctrl+alt+delete",
                "ctrl+alt+del",
                "ctrl+alt+f1",
                "ctrl+alt+f2",
                "ctrl+alt+f3",
                "ctrl+alt+f4",
                "ctrl+alt+f5",
                "ctrl+alt+f6",
                "ctrl+alt+f7",
                "ctrl+alt+f8",
            }
            if combo in dangerous_combos:
                return GuardrailResult(
                    allowed=False,
                    action="blocked",
                    reason=f"Dangerous key combination blocked: {combo}",
                    guardrail_name="desktop_safety",
                )

        # Block dangerous URLs in browser navigation
        if tool_name == "browser":
            action = tool_args.get("action", "")
            if action == "navigate":
                url = str(tool_args.get("targetUrl") or tool_args.get("url", "")).lower()
                if url.startswith("file://") or url.startswith("javascript:"):
                    return GuardrailResult(
                        allowed=False,
                        action="blocked",
                        reason=f"Blocked URL scheme: {url[:30]}",
                        guardrail_name="desktop_safety",
                    )

        return GuardrailResult()

    def _check_sensitive_output(self, tool_name: str, tool_output: Any) -> GuardrailResult:
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
