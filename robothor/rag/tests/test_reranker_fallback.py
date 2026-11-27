"""A degraded reranker is never silent.

When a pair fails, recall falls back to the fused order; when the reranker
is unavailable, rows come back unscored. Either way the results look normal,
so each is counted (``reranker.rerank_stats``) and logged at WARNING, and a
caller (the memory benchmark) can tell a degraded search from a scored one.
"""

from __future__ import annotations

import logging

import pytest

from robothor.rag import reranker


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    reranker.reset_rerank_stats()
    monkeypatch.setattr(reranker, "_max_candidates", lambda: 16)
    yield
    reranker.reset_rerank_stats()


def _rows(n=3):
    return [{"id": i, "content": f"row {i}"} for i in range(n)]


def _available(monkeypatch, value: bool):
    async def _check():
        return value

    monkeypatch.setattr(reranker, "check_reranker_available", _check)


@pytest.mark.asyncio
async def test_a_failed_pair_falls_back_logs_a_warning_and_is_counted(monkeypatch, caplog):
    _available(monkeypatch, True)

    async def _boom(_client, _q, _doc):
        raise RuntimeError("reranker 500")

    monkeypatch.setattr(reranker, "rerank_pair", _boom)
    with caplog.at_level(logging.WARNING, logger=reranker.logger.name):
        out = await reranker.rerank_with_fallback("q", _rows(), top_k=2)
    assert [r["id"] for r in out] == [0, 1], "fused order, cut to top_k"
    assert all("rerank_score" not in r for r in out)
    assert reranker.rerank_stats() == {"scored": 0, "fell_back": 1, "unavailable": 0}
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "fused order" in warning.getMessage() and "reranker 500" in warning.getMessage()
    assert "fell back 1" in warning.getMessage()


@pytest.mark.asyncio
async def test_an_unavailable_reranker_is_counted_every_time_and_warned_once(monkeypatch, caplog):
    _available(monkeypatch, False)
    with caplog.at_level(logging.WARNING, logger=reranker.logger.name):
        first = await reranker.rerank_with_fallback("q", _rows(), top_k=2)
        await reranker.rerank_with_fallback("q", _rows(), top_k=2)
    assert all(r["rerank_relevant"] == "skipped" for r in first)
    assert reranker.rerank_stats()["unavailable"] == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "unavailable" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_scored_rerank_is_counted_and_quiet(monkeypatch, caplog):
    _available(monkeypatch, True)

    async def _score(_client, _q, doc):
        return 0.9 if doc.endswith("2") else 0.1

    monkeypatch.setattr(reranker, "rerank_pair", _score)
    with caplog.at_level(logging.WARNING, logger=reranker.logger.name):
        out = await reranker.rerank_with_fallback("q", _rows(), top_k=2)
    assert out[0]["id"] == 2 and "rerank_score" in out[0]
    assert reranker.rerank_stats() == {"scored": 1, "fell_back": 0, "unavailable": 0}
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
