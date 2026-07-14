"""Prompt-injection scanner for assembled cron prompts (Rip 8).

Ported from Hermes ``cron/scheduler.py:1165-1201``. Cron-spawned
agents run unattended — a malicious skill or memory file picked up
during prompt assembly could exfiltrate data, jailbreak the agent,
or schedule more cron jobs. We scan the fully-assembled prompt for
known injection patterns before the API call and raise
``CronPromptInjectionBlocked`` to abort the spawn.
"""

from __future__ import annotations

import logging
import re

from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)


class CronPromptInjectionBlockedError(Exception):
    """Raised when a cron-spawned agent's prompt contains an injection signal."""


# Patterns ported from Hermes plus a few Genus-specific extras.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Unicode tag block — characters in the U+E0000-U+E007F range that look invisible.
    re.compile(r"[\U000E0000-\U000E007F]"),
    # Classic prompt-injection openers
    re.compile(
        r"\bignore\s+(all\s+)?previous\s+(instructions|messages|context)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdisregard\s+(all\s+)?(prior|previous|earlier)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\s+(override|update|reset)\b", re.IGNORECASE),
    # Markdown link shapes that smuggle URLs in
    re.compile(r"!\[image\]\(data:", re.IGNORECASE),
    # Command-shape indicators in unexpected places (cron-spawned shouldn't shell out)
    re.compile(r"\bexec\s*\(\s*['\"]curl\s+", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    # Cron-bootstrapping (recursive scheduling)
    re.compile(r"\bcron(job)?\s*\(", re.IGNORECASE),
)


def scan_assembled_cron_prompt(text: str) -> str | None:
    """Return the first matching pattern description, or ``None`` if clean.

    The caller can raise :class:`CronPromptInjectionBlocked` with the
    returned string, or wrap it for telemetry. This helper is a check
    only — it never raises directly so callers can decide whether to
    block, log, or run anyway under a flag.
    """
    if not text:
        return None
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return f"matched pattern: {pattern.pattern[:60]}"
    return None


def assert_safe(text: str) -> None:
    """Raise :class:`CronPromptInjectionBlocked` when the scanner trips."""
    finding = scan_assembled_cron_prompt(text)
    if finding is not None:
        raise CronPromptInjectionBlockedError(finding)


def screen_cron_prompt(text: str, *, context: str = "cron") -> str | None:
    """Scan an assembled system-run prompt and act per ``injection_scan_mode``.

    Returns the finding string when a signal is present (for the caller to log),
    else ``None``. ``off`` → no-op (returns None without scanning). ``observe``/
    ``alert`` → log a warning and return the finding but DO NOT raise. ``enforce``
    → raise :class:`CronPromptInjectionBlockedError` to abort the run.
    """
    from robothor.engine.feature_flags import injection_scan_mode

    mode = injection_scan_mode()
    if mode == "off":
        return None
    finding = scan_assembled_cron_prompt(text)
    if finding is None:
        return None
    logger.warning(
        "Injection signal in %s prompt (mode=%s): %s",
        sanitize_log(context),
        sanitize_log(mode),
        sanitize_log(finding),
    )
    if mode == "enforce":
        raise CronPromptInjectionBlockedError(finding)
    return finding
