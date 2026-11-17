"""``key=value`` log lines that a log pipeline can aggregate.

One event per line: ``event=<name>`` first, then the fields in the order given.
A value with no whitespace, quote or ``=`` is written bare; anything else is
double-quoted with ``"`` and ``\\`` escaped and newlines collapsed, so one line
never becomes two. An empty or ``None`` value is written as ``-``.

:func:`short_reason` turns error text into something safe to log: it is cut to
a bounded length BEFORE redaction (redacting megabytes of text on the event
loop is slow), redacted with :func:`robothor.secrets.redaction.redact`, and cut
to its final length afterwards. The pre-cut is many times the final length, so
a credential split at the pre-cut point lies far outside what is kept.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REASON_CHARS", "kv_line", "short_reason"]

#: Characters of a reason kept in a log line.
REASON_CHARS = 200
#: Characters of raw text handed to redaction, at most.
_PRE_REDACT_CHARS = 4096

_BARE = re.compile(r"^[^\s\"=\\]+$")
_WHITESPACE = re.compile(r"\s+")


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _BARE.match(text):
        return text
    text = _WHITESPACE.sub(" ", text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def kv_line(event: str, **fields: Any) -> str:
    """``event=<event> k1=v1 k2=v2 …``; see the module docstring for quoting."""
    parts = [f"event={_value(event)}"]
    parts += [f"{key}={_value(value)}" for key, value in fields.items()]
    return " ".join(parts)


def short_reason(text: Any, limit: int = REASON_CHARS) -> str:
    """``text`` bounded, redacted and cut to ``limit`` characters."""
    if text is None:
        return ""
    from robothor.secrets import redaction

    raw = str(text)[:_PRE_REDACT_CHARS]
    clean = _WHITESPACE.sub(" ", redaction.redact(raw)).strip()
    if len(clean) > limit:
        clean = clean[: limit - 1] + "…"
    return clean
