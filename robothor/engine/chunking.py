"""Message chunking shared by the Telegram sender and the delivery layer.

``TelegramBot.send_message`` splits long output into chunks and sends one
API call per chunk. The delivery layer needs the *same* chunk count to tell
a complete send from a partial one, so the algorithm lives here rather than
being duplicated: a drifting copy would report honest-looking but wrong
``partial:`` statuses.

``robothor.engine.telegram`` imports ``robothor.engine.delivery`` at module
scope, so this module deliberately depends on neither.
"""

from __future__ import annotations

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

#: The Helm's canvas markers. ``app/src/lib/system-prompt.ts`` asks the model
#: for ``[DASHBOARD:{json}]`` and ``[RENDER:component:{json}]`` and
#: ``marker-interceptor.ts`` swallows them in the browser — but the owner's
#: webchat and Telegram share ONE session on purpose, so the model emits them
#: on every surface. A chat has no interceptor; the marker is metadata there
#: too, not text.
_MARKER_PREFIXES = ("[DASHBOARD:", "[RENDER:")


def _marker_end(text: str, start: int) -> int:
    """Index just past the marker opening at ``start``, or ``len(text)``.

    The payload is JSON, so brackets and braces inside strings must not close
    the marker early: walk it with a depth count that ignores string contents.
    An unterminated marker (a truncated stream) runs to the end of the text —
    half a marker is still not for a person.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def strip_canvas_markers(text: str) -> str:
    """``text`` without any Helm canvas marker, and without the blank lines
    the marker left behind."""
    if not any(prefix in text for prefix in _MARKER_PREFIXES):
        return text
    sentinel = "\x00"
    out: list[str] = []
    index = 0
    while index < len(text):
        hit = min(
            (pos for pos in (text.find(prefix, index) for prefix in _MARKER_PREFIXES) if pos != -1),
            default=-1,
        )
        if hit == -1:
            out.append(text[index:])
            break
        out.append(text[index:hit])
        out.append(sentinel)
        index = _marker_end(text, hit)
    lines: list[str] = []
    for line in "".join(out).split("\n"):
        if sentinel in line and not line.replace(sentinel, "").strip():
            continue  # the marker had the line to itself: the line goes with it
        line = line.replace(f" {sentinel} ", " ").replace(sentinel, "")
        lines.append(line.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def split_telegram_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split ``text`` into chunks that fit Telegram's per-message limit.

    Prefers to break on the last newline in the window, falling back to a
    hard cut when the newline would land in the first half of the chunk
    (which would waste most of the message).

    Args:
        text: The message body to split.
        max_length: Maximum characters per chunk.

    Returns:
        The list of chunks, in send order. A message at or under the limit
        returns as a single chunk.
    """
    stripped = strip_canvas_markers(text)
    if text and not stripped:
        # Nothing but markers: nothing to send, and the delivery layer's
        # predicted chunk count (zero) agrees with the sender's.
        return []
    text = stripped
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_pos = remaining.rfind("\n", 0, max_length)
        if split_pos == -1 or split_pos < max_length // 2:
            split_pos = max_length

        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:].lstrip("\n")

    return chunks


#: The same algorithm under a surface-neutral name. Every channel that splits a
#: body must split it with THIS, and its shim must be told the limit
#: (``chunk_size``), or the two disagree about how many messages a body becomes
#: and a truncated send reads as a complete one — the drift this module was
#: extracted to prevent, one surface further out.
split_message = split_telegram_message
