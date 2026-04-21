"""
Guardrails Framework — policy enforcement for tool calls.

Runs pre-execution checks on tool calls and post-execution checks on results.
Named policies are registered globally and enabled per-agent via YAML manifest.
All events are logged to the agent_guardrail_events table.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC
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

# Human-readable descriptions for LLM prompt injection
POLICY_DESCRIPTIONS: dict[str, str] = {
    "no_destructive_writes": "Destructive shell commands (rm -rf, DROP TABLE, DELETE FROM, TRUNCATE) are blocked.",
    "no_sensitive_data": "Tool outputs are scanned for exposed API keys and secrets.",
    "rate_limit": f"Tool calls are rate-limited to {DEFAULT_RATE_LIMIT}/minute.",
    "no_external_http": "Web fetch and web search tools are blocked.",
    "no_main_branch_push": "Git push/commit to main/master branches is blocked.",
    "exec_allowlist": "Shell commands are restricted to an explicit allowlist.",
    "write_path_restrict": "File writes are restricted to specific paths.",
    "desktop_safety": "Desktop automation has additional safety checks (no terminal emulators, no dangerous key combos).",
    "human_approval": "Certain tools require explicit human approval before execution.",
    "requires_human_task_closure": (
        "If this run reads a task with requires_human=true and does not close or update it, "
        "the engine auto-marks that task IN_PROGRESS at run-end so the next heartbeat will not re-pick it. "
        "To fully close, call update_task(status=DONE) or resolve_task explicitly."
    ),
    "recurring_meeting_proposal_required": (
        "Creating a calendar invite with ≥3 external attendees, >7 days in the future, or recurring cadence "
        "is blocked unless a prior step in this run proposed the time via email, or attendee_confirmed=true."
    ),
}


def _owner_email_cached() -> str:
    """Lookup operator email for domain-classification — cheap + cache-less."""
    try:
        from robothor.engine.tools.handlers.gws import _resolve_owner_email

        return _resolve_owner_email()
    except Exception:
        import os as _os

        return _os.environ.get("ROBOTHOR_OWNER_EMAIL", "").strip().lower()


def _lookup_scheduling_policies(emails: list[str]) -> dict[str, str]:
    """Map each email to its crm_people.scheduling_policy (if non-default).

    Queries ``crm_people.email`` directly (case-insensitive). Emails not in CRM
    are omitted. Any DB / schema failure (e.g. column absent before migration)
    returns an empty dict silently — the guardrail falls back to its email
    heuristic signals only.
    """
    normalized = [e.strip().lower() for e in emails if e and "@" in e]
    if not normalized:
        return {}
    try:
        from robothor.crm.dal import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT lower(email), scheduling_policy "
                "FROM crm_people "
                "WHERE deleted_at IS NULL AND lower(email) = ANY(%s)",
                (normalized,),
            )
            rows = cur.fetchall()
    except Exception:
        return {}

    out: dict[str, str] = {}
    for row in rows or []:
        email = row[0]
        policy = (row[1] or "stable").strip()
        if policy and policy != "stable":
            out[email] = policy
    return out


def _days_from_now(start: str) -> float | None:
    """Parse an RFC3339 start string and return days between now (UTC) and then.

    Returns None if unparseable so callers can skip the check rather than crash.
    """
    from datetime import datetime

    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        return None
    now = datetime.now(tz=UTC)
    return (dt - now).total_seconds() / 86400.0


def guardrail_summary(policies: list[str]) -> str:
    """Return a concise system prompt section describing active guardrails.

    Helps the LLM self-regulate and avoid hitting guardrails blindly.
    Returns empty string if no policies are active.
    """
    if not policies:
        return ""
    lines = ["## Active Safety Guardrails"]
    for policy in policies:
        desc = POLICY_DESCRIPTIONS.get(policy, f"{policy} (custom policy)")
        lines.append(f"- {desc}")
    lines.append(
        "\nIf a tool call is blocked by a guardrail, you will receive an error. "
        "Do not attempt to work around guardrail restrictions."
    )
    return "\n".join(lines)


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
    _human_approval_patterns: dict[str, list[str]] = field(default_factory=dict)

    def check_pre_execution(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        agent_id: str = "",
        prior_steps: list[Any] | None = None,
    ) -> GuardrailResult:
        """Run all enabled pre-execution guardrails on a tool call.

        prior_steps is an optional list of this run's completed RunStep objects
        (in order). Guardrails that need to inspect context from earlier in the
        run (e.g. "did we already propose the time via email?") read it.
        """
        for policy in self.enabled_policies:
            result = self._run_pre_policy(policy, tool_name, tool_args, agent_id, prior_steps or [])
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
        prior_steps: list[Any],
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
        if policy == "human_approval":
            return self._check_human_approval(tool_name, tool_args, agent_id)
        if policy == "recurring_meeting_proposal_required":
            return self._check_recurring_meeting_proposal(tool_name, tool_args, prior_steps)
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
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
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

    def set_human_approval_patterns(self, agent_id: str, patterns: list[str]) -> None:
        """Configure tool patterns that require human approval for an agent."""
        self._human_approval_patterns[agent_id] = patterns

    def _check_human_approval(
        self, tool_name: str, tool_args: dict[str, Any], agent_id: str
    ) -> GuardrailResult:
        """Escalate tool calls that match human_approval_tools patterns."""
        patterns = self._human_approval_patterns.get(agent_id, [])
        if not patterns:
            return GuardrailResult()
        for pattern in patterns:
            if fnmatch.fnmatch(tool_name, pattern):
                return GuardrailResult(
                    allowed=False,
                    action="escalate",
                    reason=f"Tool '{tool_name}' requires human approval",
                    guardrail_name="human_approval",
                )
        return GuardrailResult()

    def _check_recurring_meeting_proposal(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        prior_steps: list[Any],
    ) -> GuardrailResult:
        """Block high-stakes calendar invites that were not pre-proposed via email.

        High-stakes = ≥3 external-domain attendees OR starts >7d out OR has recurrence.
        Accepted evidence of proposal: a prior gws_gmail_send/gws_gmail_reply step
        in this run whose body contains time-proposing language. Alternatively,
        the caller can pass ``attendee_confirmed=true`` to certify the proposal
        happened out-of-band.
        """
        if tool_name != "gws_calendar_create":
            return GuardrailResult()

        if tool_args.get("force") or tool_args.get("attendee_confirmed"):
            return GuardrailResult()

        # Decide whether this invite is "high-stakes".
        attendees = tool_args.get("attendees", []) or []
        owner_email = _owner_email_cached()
        owner_domain = owner_email.split("@", 1)[1] if "@" in owner_email else ""
        external_attendee_count = 0
        for a in attendees:
            if not isinstance(a, str) or "@" not in a:
                continue
            dom = a.split("@", 1)[1].lower()
            if dom and dom != owner_domain:
                external_attendee_count += 1

        # Check CRM scheduling_policy for each attendee.
        policies = _lookup_scheduling_policies([a for a in attendees if isinstance(a, str)])
        if "no_auto" in policies.values():
            blocked_person = next(email for email, p in policies.items() if p == "no_auto")
            return GuardrailResult(
                allowed=False,
                action="blocked",
                reason=(
                    f"Blocked — {blocked_person} has scheduling_policy='no_auto'. "
                    "Agents may not create calendar invites for this person; "
                    "only the operator can. If the operator has approved this "
                    "invite out-of-band, pass force=true."
                ),
                guardrail_name="recurring_meeting_proposal_required",
            )
        attendee_needs_proposal = "ask_first" in policies.values()

        start = tool_args.get("start", "")
        days_out = _days_from_now(start)
        has_recurrence = bool(tool_args.get("recurrence"))

        high_stakes = (
            attendee_needs_proposal
            or external_attendee_count >= 3
            or (days_out is not None and days_out > 7)
            or has_recurrence
        )
        if not high_stakes:
            return GuardrailResult()

        # Look for a proposal step in this run.
        proposal_tools = {"gws_gmail_send", "gws_gmail_reply"}
        proposal_keywords = (
            "propose",
            "suggest",
            "availability",
            "available",
            "work for you",
            "would this work",
            "would that work",
            "does this work",
            "does that work",
            "please confirm",
            "let me know a time",
            "prefer",
        )
        for step in prior_steps:
            if getattr(step, "tool_name", None) not in proposal_tools:
                continue
            args = getattr(step, "tool_input", None) or {}
            body = str(args.get("body", "")).lower()
            if any(kw in body for kw in proposal_keywords):
                return GuardrailResult()

        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=(
                "Blocked high-stakes calendar invite — no time-proposal email was "
                "sent in this run. Send a 'does X work for you?' email first, then "
                "create the event after attendees confirm; or pass "
                "attendee_confirmed=true or force=true to certify an out-of-band "
                "confirmation."
            ),
            guardrail_name="recurring_meeting_proposal_required",
        )

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


# ─── Post-run guardrails ─────────────────────────────────────────────
#
# Post-run checks don't belong on GuardrailEngine (which is per-tool-call) —
# they operate on a finished run and can enqueue side-effects like task updates.


def _collect_driven_task_ids(run: Any) -> set[str]:
    """Task IDs this run read as requires_human=true, plus any run.task_id.

    Looks at each `get_task` step's output for `requires_human` / `requiresHuman`
    (both spellings are produced by different DAL code paths).
    """
    driven: set[str] = set()
    task_id = getattr(run, "task_id", None)
    if task_id:
        driven.add(str(task_id))
    for step in getattr(run, "steps", []) or []:
        if getattr(step, "tool_name", None) != "get_task":
            continue
        out = getattr(step, "tool_output", None)
        if not isinstance(out, dict):
            continue
        flag = out.get("requires_human")
        if flag is None:
            flag = out.get("requiresHuman")
        if not flag:
            continue
        tid = out.get("id")
        if tid:
            driven.add(str(tid))
    return driven


def _collect_closed_task_ids(run: Any) -> set[str]:
    """Task IDs the run already closed or moved out of TODO via update_task/resolve_task."""
    closed: set[str] = set()
    for step in getattr(run, "steps", []) or []:
        name = getattr(step, "tool_name", None)
        if name not in ("update_task", "resolve_task"):
            continue
        args = getattr(step, "tool_input", None) or {}
        tid = args.get("id") or args.get("task_id")
        if not tid:
            continue
        if name == "resolve_task":
            closed.add(str(tid))
            continue
        # update_task: only counts as closure if it set a non-TODO status
        status = str(args.get("status", "")).upper()
        if status in {"DONE", "REVIEW", "IN_PROGRESS", "CANCELLED", "BLOCKED"}:
            closed.add(str(tid))
    return closed


def check_post_run(run: Any, agent_config: Any, tenant_id: str = "") -> list[str]:
    """Post-run enforcement — currently: requires_human_task_closure.

    If the agent has the ``requires_human_task_closure`` guardrail enabled and
    the run read any requires_human task without closing it, flip those tasks
    to IN_PROGRESS with a note tying them to this run, so the next heartbeat
    does not re-pick them.

    Returns the list of task IDs that were auto-advanced (for logging/tests).
    Silent no-op if the guardrail is not enabled for this agent.
    """
    policies = getattr(agent_config, "guardrails", []) or []
    if "requires_human_task_closure" not in policies:
        return []

    driven = _collect_driven_task_ids(run)
    if not driven:
        return []
    closed = _collect_closed_task_ids(run)
    unclosed = driven - closed
    if not unclosed:
        return []

    run_id = getattr(run, "id", "")
    agent_id = getattr(run, "agent_id", "")
    marker = (
        f"\n[{agent_id} auto-advanced by run {run_id}: "
        f"run read this requires_human task but did not close it explicitly]"
    )

    advanced: list[str] = []
    try:
        from robothor.crm.dal import DEFAULT_TENANT, get_task, update_task
    except Exception as e:
        logger.warning("check_post_run: crm.dal import failed: %s", e)
        return []

    tid_tenant = tenant_id or DEFAULT_TENANT
    for tid in unclosed:
        try:
            existing = get_task(tid, tenant_id=tid_tenant)
            if not existing:
                continue
            # Never regress a task that's already past TODO.
            current_status = str(existing.get("status", "")).upper()
            if current_status != "TODO":
                continue
            body = (existing.get("body") or "") + marker
            ok = update_task(
                tid,
                changed_by=agent_id,
                tenant_id=tid_tenant,
                status="IN_PROGRESS",
                body=body,
            )
            if ok:
                advanced.append(tid)
                logger.warning(
                    "requires_human_task_closure: auto-advanced task %s to IN_PROGRESS "
                    "(driven_by_run=%s agent=%s)",
                    tid,
                    run_id,
                    agent_id,
                )
        except Exception as e:
            logger.warning("requires_human_task_closure: failed to advance task %s: %s", tid, e)
    return advanced
