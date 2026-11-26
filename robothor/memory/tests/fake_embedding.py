"""A deterministic stand-in for the embedding model, for graph-relevance tests.

Words map to dimensions; a few words that mean the same thing share one, so a
paraphrase ("based", "headquartered", "office") lands on the fact it means
without sharing a word with it. Every text also carries a small constant
component, so unrelated texts have a low but non-zero similarity. Not a model:
it only lets a test say "these two mean the same, those do not".
"""

from __future__ import annotations

import hashlib
import math
import re

DIMS = 1024

#: Words of one meaning share a dimension.
SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"headquartered", "headquarters", "based", "base", "office", "hq"}),
    frozenset({"city", "town"}),
    frozenset({"boss", "manager", "reports", "reported", "report"}),
    frozenset({"door", "code", "keypad"}),
    frozenset({"memo", "note"}),
)

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "by",
        "with",
        "and",
        "or",
        "which",
        "what",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "how",
        "does",
        "do",
        "did",
        "has",
        "have",
        "had",
        "it",
        "its",
        "this",
        "that",
        "as",
        "from",
        "s",
    ]
)


def _dim(word: str) -> int:
    for i, group in enumerate(SYNONYMS):
        if word in group:
            return i
    return len(SYNONYMS) + int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % (
        DIMS - len(SYNONYMS) - 1
    )


def fake_embed(text: str) -> list[float]:
    # A query arrives with the model's instruction in front ("Instruct: ...
    # Query:..."); like the real model, only what is asked carries meaning.
    text = (text or "").rsplit("Query:", 1)[-1]
    v = [0.0] * DIMS
    v[DIMS - 1] = 0.05
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w not in STOPWORDS:
            d = _dim(w)
            # A shared meaning weighs more than any one other word.
            v[d] += 2.0 if d < len(SYNONYMS) else 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def embed_all_facts(sql) -> None:
    """Give every fact without an embedding its fake one (``sql`` runs a statement)."""
    for fid, text in sql("SELECT id, fact_text FROM memory_facts WHERE embedding IS NULL"):
        sql(
            "UPDATE memory_facts SET embedding = %s::vector WHERE id = %s",
            (str(fake_embed(text)), fid),
        )
