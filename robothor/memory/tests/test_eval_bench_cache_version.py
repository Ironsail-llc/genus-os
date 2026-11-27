"""A cached context belongs to the code and flags that retrieved it.

A context is reused only when nothing that decides retrieval changed: the
memory package, the systems module, the reranker, the memory flags of the
process and, for the product-parity cell, the tool handler and the code that
builds the tool message. Scoring and report files stay out, so a report
change never throws contexts away.
"""

from __future__ import annotations

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _fake_ingest, _FakeChat


@pytest.fixture
def repo(tmp_path):
    files = {
        "robothor/memory/recall.py": "A = 1\n",
        "robothor/memory/eval_bench.py": "B = 1\n",
        "robothor/memory/eval_hosted.py": "B = 1\n",
        "robothor/memory/eval_systems.py": "C = 1\n",
        "robothor/rag/reranker.py": "D = 1\n",
        "robothor/engine/tools/handlers/memory.py": "E = 1\n",
        "robothor/engine/session.py": "F = 1\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _edit(repo, rel):
    path = repo / rel
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")


@pytest.mark.parametrize(
    "rel",
    ["robothor/memory/recall.py", "robothor/memory/eval_systems.py", "robothor/rag/reranker.py"],
)
def test_an_edit_to_retrieval_code_misses_the_cache(repo, rel):
    before = eb.context_version("recall", repo_root=repo)
    _edit(repo, rel)
    assert eb.context_version("recall", repo_root=repo) != before


def test_scoring_and_report_edits_keep_the_cache(repo):
    before = {s: eb.context_version(s, repo_root=repo) for s in ("recall", "search_memory_tool")}
    _edit(repo, "robothor/memory/eval_bench.py")
    _edit(repo, "robothor/memory/eval_hosted.py")
    assert {s: eb.context_version(s, repo_root=repo) for s in before} == before


@pytest.mark.parametrize(
    "rel", ["robothor/engine/tools/handlers/memory.py", "robothor/engine/session.py"]
)
def test_the_tool_path_versions_the_parity_cell_only(repo, rel):
    parity = eb.context_version("search_memory_tool", repo_root=repo)
    harness = eb.context_version("recall", repo_root=repo)
    _edit(repo, rel)
    assert eb.context_version("search_memory_tool", repo_root=repo) != parity
    assert eb.context_version("recall", repo_root=repo) == harness


@pytest.mark.parametrize(
    "flag",
    [
        "MEMORY_NARROW_SEARCH",
        "ROBOTHOR_RERANKER_MODEL",
        # The panic switch turns routed recall off; the HNSW knobs change the
        # vector legs' candidate pool.
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "MEMORY_HNSW_EF_SEARCH",
        "MEMORY_HNSW_ITERATIVE",
        "MEMORY_HNSW_MAX_SCAN_TUPLES",
    ],
)
def test_a_flag_change_misses_the_cache(repo, monkeypatch, flag):
    monkeypatch.delenv(flag, raising=False)
    before = {s: eb.context_version(s, repo_root=repo) for s in ("recall", "search_memory_tool")}
    monkeypatch.setenv(flag, "1")
    after = {s: eb.context_version(s, repo_root=repo) for s in before}
    assert all(after[s] != before[s] for s in before)


class _Counting:
    name = "counting"
    equal_budget = True
    calls = 0

    async def retrieve(self, case, *, world_handle, token_budget):
        type(self).calls += 1
        return es.ContextBundle(
            self.name,
            es.fill_budget([("s0#0u", "turn", "User: I took up the cello.")], token_budget),
            token_budget,
            True,
            False,
            1.0,
        )


@pytest.mark.asyncio
async def test_a_run_after_a_flag_change_retrieves_again(tmp_path, monkeypatch):
    es.register_system("counting", _Counting)
    _Counting.calls = 0
    monkeypatch.delenv("MEMORY_NARROW_SEARCH", raising=False)

    async def _run():
        return await eb.run_benchmark(
            _bench(1),
            systems=["counting"],
            cache_dir=str(tmp_path),
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(),
            validate_judge=False,
        )

    try:
        await _run()
        await _run()
        assert _Counting.calls == 1, "same code and flags: served from the cache"
        monkeypatch.setenv("MEMORY_NARROW_SEARCH", "1")
        report = await _run()
        assert _Counting.calls == 2, "a flag change is retrieved again"
    finally:
        es.unregister_system("counting")
    assert report["params"]["context_versions"]["counting"] == eb.context_version("counting")
