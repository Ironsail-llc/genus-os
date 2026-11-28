"""The on-disk embedding cache: hits across instances, misses on a model or
schema change, never inside the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.memory import eval_embed_cache as ec


class Counting:
    def __init__(self) -> None:
        self.calls = 0
        self.texts: list[str] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)
        return [[float(len(t)), float(i), 0.5] for i, t in enumerate(texts)]


def _cache(tmp_path: Path, inner: Counting, **kw) -> ec.CachedEmbedder:
    params = {"model": "embed-a", "seed": 1, "size": "small", "schema_version": 1}
    params.update(kw)
    return ec.CachedEmbedder(inner, cache_dir=tmp_path, **params)


class TestCachedEmbedder:
    @pytest.mark.asyncio
    async def test_second_instance_reads_from_disk(self, tmp_path):
        first = Counting()
        a = _cache(tmp_path, first)
        out1 = await a(["alpha", "beta"])
        a.save()
        second = Counting()
        b = _cache(tmp_path, second)
        out2 = await b(["beta", "alpha"])
        assert second.calls == 0
        assert out2 == [out1[1], out1[0]]
        assert b.hits == 2 and b.misses == 0

    @pytest.mark.asyncio
    async def test_only_missing_texts_are_embedded(self, tmp_path):
        inner = Counting()
        c = _cache(tmp_path, inner)
        await c(["a", "b"])
        await c(["b", "c"])
        assert inner.texts == ["a", "b", "c"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change", [{"model": "embed-b"}, {"schema_version": 2}, {"seed": 2}, {"size": "full"}]
    )
    async def test_model_schema_seed_or_size_change_invalidates(self, tmp_path, change):
        a = _cache(tmp_path, Counting())
        await a(["alpha"])
        a.save()
        inner = Counting()
        b = _cache(tmp_path, inner, **change)
        await b(["alpha"])
        assert inner.calls == 1

    @pytest.mark.asyncio
    async def test_vectors_round_trip_as_float32(self, tmp_path):
        a = _cache(tmp_path, Counting())
        await a(["hello"])
        a.save()
        b = _cache(tmp_path, Counting())
        (vec,) = await b(["hello"])
        assert vec == pytest.approx([5.0, 0.0, 0.5])

    @pytest.mark.asyncio
    async def test_default_dir_is_outside_the_repo_and_overridable(self, monkeypatch, tmp_path):
        """Overridden by argument (the CLI's --cache-dir), never by a raw env read:
        configuration goes through robothor.settings, and the env-read ratchet
        must not grow for an eval-only path."""
        repo = Path(__file__).resolve().parents[3]
        default = ec.default_cache_dir()
        assert repo not in default.resolve().parents
        assert default == Path.home() / ".cache" / "genus-os" / "memory-eval"
        monkeypatch.setenv("ROBOTHOR_MEMORY_EVAL_CACHE_DIR", str(tmp_path / "ignored"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "ignored"))
        assert ec.default_cache_dir() == default
        c = _cache(tmp_path / "x", Counting())
        await c(["alpha"])
        c.save()
        assert any((tmp_path / "x").iterdir())

    @pytest.mark.asyncio
    async def test_a_corrupt_cache_file_is_ignored(self, tmp_path):
        a = _cache(tmp_path, Counting())
        await a(["alpha"])
        a.save()
        for f in tmp_path.rglob("*"):
            if f.is_file():
                f.write_bytes(b"garbage")
        inner = Counting()
        b = _cache(tmp_path, inner)
        await b(["alpha"])
        assert inner.calls == 1
