"""
Error Recovery — classify tool errors and determine recovery actions.

Parses error messages into typed categories (auth, rate limit, timeout, etc.)
and returns appropriate recovery actions (spawn helper, backoff, retry, inject).
Used by the runner to autonomously recover from tool failures instead of
immediately escalating to the human.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from robothor.engine.models import ErrorType, RecoveryAction

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

# ─── Error classification patterns ───────────────────────────────────

_AUTH_PATTERNS = [
    re.compile(r"40[13]\b", re.IGNORECASE),
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"forbidden", re.IGNORECASE),
    re.compile(r"invalid.{0,20}token", re.IGNORECASE),
    re.compile(r"invalid.{0,20}key", re.IGNORECASE),
    re.compile(r"authentication.{0,20}fail", re.IGNORECASE),
    re.compile(r"access.{0,20}denied", re.IGNORECASE),
]

_RATE_LIMIT_PATTERNS = [
    re.compile(r"429\b"),
    re.compile(r"rate.{0,10}limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"quota.{0,20}exceed", re.IGNORECASE),
    re.compile(r"throttl", re.IGNORECASE),
]

_NOT_FOUND_PATTERNS = [
    re.compile(r"404\b"),
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"FileNotFoundError", re.IGNORECASE),
    re.compile(r"No such file", re.IGNORECASE),
    re.compile(r"does not exist", re.IGNORECASE),
    re.compile(r"ModuleNotFoundError", re.IGNORECASE),
]

_TIMEOUT_PATTERNS = [
    re.compile(r"TimeoutError", re.IGNORECASE),
    re.compile(r"timed?\s*out", re.IGNORECASE),
    re.compile(r"deadline.{0,10}exceed", re.IGNORECASE),
    re.compile(r"ETIMEDOUT", re.IGNORECASE),
    re.compile(r"ConnectTimeoutError", re.IGNORECASE),
]

_DEPENDENCY_PATTERNS = [
    re.compile(r"ModuleNotFoundError", re.IGNORECASE),
    re.compile(r"ImportError", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"No module named", re.IGNORECASE),
    re.compile(r"package.{0,20}not.{0,10}install", re.IGNORECASE),
]

_PERMISSION_PATTERNS = [
    re.compile(r"PermissionError", re.IGNORECASE),
    re.compile(r"EACCES", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"Operation not permitted", re.IGNORECASE),
]

_API_DEPRECATED_PATTERNS = [
    re.compile(r"deprecated", re.IGNORECASE),
    re.compile(r"removed.{0,20}api", re.IGNORECASE),
    re.compile(r"no longer.{0,10}support", re.IGNORECASE),
    re.compile(r"410\b"),
]

# Ordered by specificity — dependency before not_found (ModuleNotFoundError
# matches both, but dependency is the more specific classification)
_PATTERN_MAP: list[tuple[list[re.Pattern[str]], ErrorType]] = [
    (_DEPENDENCY_PATTERNS, ErrorType.DEPENDENCY),
    (_AUTH_PATTERNS, ErrorType.AUTH),
    (_RATE_LIMIT_PATTERNS, ErrorType.RATE_LIMIT),
    (_TIMEOUT_PATTERNS, ErrorType.TIMEOUT),
    (_PERMISSION_PATTERNS, ErrorType.PERMISSION),
    (_API_DEPRECATED_PATTERNS, ErrorType.API_DEPRECATED),
    (_NOT_FOUND_PATTERNS, ErrorType.NOT_FOUND),
]


def classify_error(tool_name: str, error_msg: str) -> ErrorType:
    """Classify an error message into a typed category.

    Uses regex pattern matching against common error patterns.
    Returns ErrorType.UNKNOWN if no pattern matches.
    """
    if not error_msg:
        return ErrorType.UNKNOWN

    for patterns, error_type in _PATTERN_MAP:
        for pattern in patterns:
            if pattern.search(error_msg):
                return error_type

    return ErrorType.UNKNOWN


# ─── Recovery action logic ───────────────────────────────────────────

# Max helper agent spawns per run
MAX_HELPER_SPAWNS = 2


def get_recovery_action(
    error_type: ErrorType,
    consecutive_count: int,
    agent_config: AgentConfig,
    tool_name: str = "",
    error_msg: str = "",
    helper_spawns_used: int = 0,
) -> RecoveryAction | None:
    """Determine the appropriate recovery action for a classified error.

    Returns None if no special recovery is needed (fall through to
    standard error feedback / escalation).

    Args:
        error_type: The classified error type
        consecutive_count: How many consecutive errors of this type
        agent_config: The agent's configuration (for can_spawn_agents check)
        tool_name: The tool that failed
        error_msg: The original error message (for spawn context)
        helper_spawns_used: How many helper spawns already used this run
    """
    can_spawn = agent_config.can_spawn_agents and helper_spawns_used < MAX_HELPER_SPAWNS

    if error_type == ErrorType.RATE_LIMIT:
        # Exponential backoff, never spawn for rate limits
        delay = min(2**consecutive_count, 30)
        return RecoveryAction(
            action="backoff",
            delay_seconds=delay,
            message=f"Rate limited on {tool_name}. Waiting {delay}s before retry.",
        )

    if error_type == ErrorType.AUTH:
        if consecutive_count >= 2 and can_spawn:
            return RecoveryAction(
                action="spawn",
                agent_id="main",
                message=(
                    f"Diagnose authentication failure for tool '{tool_name}': {error_msg}\n"
                    "Check if credentials are expired, tokens need refresh, or "
                    "an alternative authentication method is available."
                ),
            )
        return None  # fall through to standard escalation

    if error_type == ErrorType.NOT_FOUND:
        if consecutive_count >= 2 and can_spawn:
            return RecoveryAction(
                action="spawn",
                agent_id="main",
                message=(
                    f"Resource not found for tool '{tool_name}': {error_msg}\n"
                    "Find an alternative path, resource, or approach to "
                    "accomplish the same goal."
                ),
            )
        return None

    if error_type == ErrorType.TIMEOUT:
        if consecutive_count == 1:
            return RecoveryAction(
                action="retry",
                message=f"Timeout on {tool_name} — retrying once.",
            )
        if consecutive_count >= 2 and can_spawn:
            return RecoveryAction(
                action="spawn",
                agent_id="main",
                message=(
                    f"Repeated timeout on tool '{tool_name}': {error_msg}\n"
                    "Find an alternative approach that avoids this tool "
                    "or reduces the operation scope."
                ),
            )
        return None

    if error_type == ErrorType.DEPENDENCY:
        if consecutive_count >= 1 and can_spawn:
            return RecoveryAction(
                action="spawn",
                agent_id="main",
                message=(
                    f"Missing dependency for tool '{tool_name}': {error_msg}\n"
                    "Install the missing package or find an alternative tool."
                ),
            )
        return None

    if error_type == ErrorType.PERMISSION:
        # Inject guidance — don't spawn (permissions usually need human)
        return RecoveryAction(
            action="inject",
            message=(
                f"Permission denied on {tool_name}: {error_msg}\n"
                "Try an alternative approach that doesn't require these permissions, "
                "or use a different file path / resource."
            ),
        )

    # LOGIC, API_DEPRECATED, UNKNOWN — fall through to standard escalation
    return None
