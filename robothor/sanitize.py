"""Universal sanitization — prevent injection from user-controlled data.

Replaces inline ``_LOG_SANITIZE_TABLE`` / ``_sanitize()`` definitions that were
duplicated in runner.py, config.py, and workflow.py. Import from here instead.

Two destinations, one reason. ``sanitize_log`` protects a log record's
structure; ``sanitize_preview`` protects an operator-facing NOTIFICATION's
structure, which is the same defect with a human reading the forged line
instead of a parser. The second lived inside ``engine/telegram.py`` -- the
module this repo caps hardest -- although nothing in it is about Telegram.

Usage::

    from robothor.engine.sanitize import sanitize_log

    logger.warning("Tool %s failed: %s", tool_name, sanitize_log(error))
"""

from __future__ import annotations

import re

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


def sanitize_preview(text: str, max_len: int = 100) -> str:
    """Collapse newlines/control characters to spaces and cap length.

    Used to embed untrusted, attacker-controlled message text (an
    unregistered sender's raw message) inside an operator-facing
    notification (review Finding 2). Without this, a crafted multi-line
    message could plant its own fake "To register them:" line followed by a
    bogus CLI command, made to look identical to the genuine registration
    hint that follows — tricking the operator into copy-pasting an
    attacker-supplied command. Stripping every line break and control
    character confines the preview to a single line no matter what the
    sender sent, so it can never spawn a second line that impersonates the
    notification's own structure.

    It also drops everything from a ``/secure`` marker onwards, the same cut
    ``robothor.secrets.redaction.redact`` makes for a log line. The preview is
    the sender's raw text rendered into a Telegram message in the OPERATOR's
    chat, which is the one place the private-input boundary exists to keep a
    payload out of — and an unregistered sender's ``/secure credential …
    {"password": …}`` now reaches this function, because that boundary routes
    a stranger through the ordinary unregistered-sender path so the operator
    is actually told about them.
    """
    # Imported here, not at module scope: `sanitize_log` above is pulled in by
    # crm/dal, the scheduler and six bridge routers, and `redaction`'s compiled
    # credential patterns cost ~25ms that none of them has a use for. This
    # function runs once per message from a stranger.
    from robothor.secrets.redaction import cut_private_input

    cut = cut_private_input(text, "[private input withheld]")
    collapsed = re.sub(r"[\r\n\t\x00-\x1f\x7f]+", " ", cut)
    return collapsed.strip()[:max_len]
