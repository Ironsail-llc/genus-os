"""Universal log sanitization — prevent log injection from user-controlled data.

Replaces inline ``_LOG_SANITIZE_TABLE`` / ``_sanitize()`` definitions that were
duplicated in runner.py, config.py, and workflow.py. Import from here instead.

Usage::

    from robothor.engine.sanitize import sanitize_log

    logger.warning("Tool %s failed: %s", tool_name, sanitize_log(error))
"""

from __future__ import annotations

_LOG_SANITIZE_TABLE = str.maketrans({"\n": "\\n", "\r": "\\r"})


def sanitize_log(val: object) -> str:
    """Sanitize a value for safe inclusion in log messages.

    Escapes newlines and carriage returns to prevent log injection attacks
    where user-controlled data could split log entries.
    """
    return str(val).translate(_LOG_SANITIZE_TABLE)
