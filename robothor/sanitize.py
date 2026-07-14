"""Universal log sanitization — prevent log injection from user-controlled data.

Replaces inline ``_LOG_SANITIZE_TABLE`` / ``_sanitize()`` definitions that were
duplicated in runner.py, config.py, and workflow.py. Import from here instead.

Usage::

    from robothor.engine.sanitize import sanitize_log

    logger.warning("Tool %s failed: %s", tool_name, sanitize_log(error))
"""

from __future__ import annotations

_LOG_SANITIZE_TABLE = str.maketrans(
    {
        chr(codepoint): (
            "\\n" if codepoint == 0x0A else "\\r" if codepoint == 0x0D else f"\\x{codepoint:02x}"
        )
        for codepoint in (*range(0x20), *range(0x7F, 0xA0))
    }
)


def sanitize_log(val: object) -> str:
    """Sanitize a value for safe inclusion in log messages.

    Escapes record separators and every C0/C1 control character so
    user-controlled data cannot split or visually manipulate log entries.
    """
    return str(val).translate(_LOG_SANITIZE_TABLE)
