"""Shared fixtures for the knowledge-base tests: a deterministic embedder."""

from __future__ import annotations

import hashlib
import math
import re

import pytest

#: Same width as memory_facts.embedding, so the fake vectors fit kb_chunks too.
DIM = 1024


def fake_vector(text: str) -> list[float]:
    """Bag-of-words hashed into DIM buckets, L2-normalised.

    Deterministic across processes (md5, not ``hash()``), and texts that share
    words point the same way, so cosine similarity means something.
    """
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeEmbedder:
    """An async ``embed`` callable that counts its calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        return fake_vector(text)


@pytest.fixture
def embed() -> FakeEmbedder:
    return FakeEmbedder()
