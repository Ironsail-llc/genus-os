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
