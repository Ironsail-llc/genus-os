"""First-party ripgrep code search (Wave-2, code intelligence).

Adds search_files so the self-improvement loop (agent-architect, Nightwatch)
finds code via a first-party tool instead of shelling out through exec.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from robothor.engine.tools.handlers.filesystem import _search_files
from robothor.engine.tools.schemas import get_engine_schemas


def _ctx(workspace):
    return SimpleNamespace(workspace=str(workspace), agent_id="a", tenant_id="default")


async def test_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    out = await _search_files({"pattern": "hello", "glob": "*.py"}, _ctx(tmp_path))
    assert out["count"] == 1
    assert out["matches"][0]["file"].endswith("a.py")
    assert out["matches"][0]["line"] == 1
    assert "hello" in out["matches"][0]["text"]


async def test_no_matches_is_not_an_error(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n")
    out = await _search_files({"pattern": "zzz_not_present"}, _ctx(tmp_path))
    assert out["count"] == 0
    assert "error" not in out


async def test_requires_pattern(tmp_path):
    assert "error" in await _search_files({}, _ctx(tmp_path))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
async def test_symlink_escaping_workspace_is_not_read(tmp_path):
    """A symlink inside the workspace pointing at a secret outside it must not
    have its contents scanned/returned."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SUPERSECRET token here\n")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ok.py").write_text("SUPERSECRET is not here\n")
    # A symlink living inside the workspace but pointing outside it.
    (workspace / "leak.txt").symlink_to(secret)

    out = await _search_files({"pattern": "SUPERSECRET"}, _ctx(workspace))
    files = {m["file"] for m in out["matches"]}
    assert not any("leak" in f for f in files), f"symlink target leaked: {out}"
    # The genuine in-workspace file still matches.
    assert any(f.endswith("ok.py") for f in files)


async def test_single_file_truncation_flag(tmp_path):
    """The single-file branch reports truncated=True when max_results is hit."""
    big = tmp_path / "many.txt"
    big.write_text("hit\n" * 50)
    out = await _search_files(
        {"pattern": "hit", "path": "many.txt", "max_results": 5}, _ctx(tmp_path)
    )
    assert out["count"] == 5
    assert out["truncated"] is True


def test_schema_registered():
    assert "search_files" in get_engine_schemas()
