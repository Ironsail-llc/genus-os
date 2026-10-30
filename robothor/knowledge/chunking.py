"""Split a document into overlapping windows for embedding.

A "token" here is a whitespace-separated word: close enough to a model token
for sizing a window, and free of any tokenizer dependency, so the same text
always yields the same chunks. Whitespace inside a window is kept as written,
so a chunk handed back to an agent still reads like the source.
"""

from __future__ import annotations

import re

__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_OVERLAP", "chunk_text"]

DEFAULT_MAX_TOKENS = 400
DEFAULT_OVERLAP = 40

# A word and the whitespace after it.
_TOKEN = re.compile(r"\S+\s*")


def chunk_text(
    text: str, max_tokens: int = DEFAULT_MAX_TOKENS, overlap: int = DEFAULT_OVERLAP
) -> list[str]:
    """Windows of at most ``max_tokens`` words; each repeats the previous one's
    last ``overlap`` words. Blank text has no chunks."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be at least 1")
    if not 0 <= overlap < max_tokens:
        raise ValueError("overlap must be at least 0 and less than max_tokens")

    tokens = _TOKEN.findall(text.strip())
    if not tokens:
        return []

    step = max_tokens - overlap
    chunks: list[str] = []
    start = 0
    while True:
        window = tokens[start : start + max_tokens]
        chunks.append("".join(window).rstrip())
        if start + max_tokens >= len(tokens):
            return chunks
        start += step
