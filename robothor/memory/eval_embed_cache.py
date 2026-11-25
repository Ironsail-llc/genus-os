"""On-disk embedding cache for the eval world.

Seeding the full world embeds a few thousand facts, document chunks and
roughly 6,500 transcript turns. Without a cache, every repeated run pays for
all of that again. Vectors are keyed by a hash of the text and stored per
``(embedding model, world schema version, seed, size)``, so a change to any
of those reads a different file, and a stale vector is never served.

The cache lives outside the repository: ``~/.cache/genus-os/memory-eval`` by
default, overridable with ``cache_dir`` (the CLI's ``--cache-dir``). Vectors are
stored as float32; a file that cannot be read is ignored and rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from array import array
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

EmbedBatch = Callable[[list[str]], Awaitable[list[list[float]]]]


def default_cache_dir() -> Path:
    """``~/.cache/genus-os/memory-eval``. Override with ``cache_dir`` (``--cache-dir``)."""
    return Path.home() / ".cache" / "genus-os" / "memory-eval"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:80]


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CachedEmbedder:
    """Wrap a batch embedder; embed only texts not already on disk."""

    def __init__(
        self,
        inner: EmbedBatch,
        *,
        model: str,
        seed: int,
        size: str,
        schema_version: int,
        cache_dir: Path | None = None,
    ) -> None:
        self.inner = inner
        self.model = model
        root = cache_dir if cache_dir is not None else default_cache_dir()
        digest = hashlib.sha256(f"{model}|{schema_version}|{seed}|{size}".encode()).hexdigest()
        stem = f"{_slug(model)}-v{schema_version}-s{seed}-{size}-{digest[:12]}"
        self.index_path = Path(root) / f"{stem}.json"
        self.data_path = Path(root) / f"{stem}.f32"
        self.vectors: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not (self.index_path.exists() and self.data_path.exists()):
            return
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            dim = int(index["dim"])
            keys = list(index["keys"])
            flat = array("f")
            flat.frombytes(self.data_path.read_bytes())
            if dim <= 0 or len(flat) != dim * len(keys):
                raise ValueError("vector count does not match the index")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("ignoring unreadable embedding cache %s: %s", self.index_path, exc)
            return
        for i, key in enumerate(keys):
            self.vectors[key] = list(flat[i * dim : (i + 1) * dim])

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        keys = [_key(t) for t in texts]
        missing: list[str] = []
        seen: set[str] = set()
        for text, key in zip(texts, keys, strict=True):
            if key not in self.vectors and key not in seen:
                missing.append(text)
                seen.add(key)
        self.misses += sum(1 for k in keys if k not in self.vectors)
        self.hits += sum(1 for k in keys if k in self.vectors)
        if missing:
            vectors = await self.inner(missing)
            if len(vectors) != len(missing):
                raise RuntimeError(
                    f"embedder returned {len(vectors)} vectors for {len(missing)} texts"
                )
            for text, vec in zip(missing, vectors, strict=True):
                self.vectors[_key(text)] = [float(x) for x in vec]
            self._dirty = True
        return [self.vectors[k] for k in keys]

    async def one(self, text: str) -> list[float]:
        return (await self([text]))[0]

    def save(self) -> None:
        if not self._dirty or not self.vectors:
            return
        keys = sorted(self.vectors)
        dim = len(self.vectors[keys[0]])
        flat = array("f")
        for key in keys:
            vec = self.vectors[key]
            if len(vec) != dim:
                raise ValueError("embedding cache holds vectors of different widths")
            flat.extend(vec)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_data = self.data_path.with_suffix(".f32.tmp")
        tmp_index = self.index_path.with_suffix(".json.tmp")
        tmp_data.write_bytes(flat.tobytes())
        tmp_index.write_text(
            json.dumps({"model": self.model, "dim": dim, "keys": keys}), encoding="utf-8"
        )
        tmp_data.replace(self.data_path)
        tmp_index.replace(self.index_path)
        self._dirty = False
