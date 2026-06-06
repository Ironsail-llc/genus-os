"""First-party ripgrep code search (Wave-2, code intelligence).

Adds search_files so the self-improvement loop (agent-architect, Nightwatch)
finds code via a first-party tool instead of shelling out through exec.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_schema_registered():
    assert "search_files" in get_engine_schemas()
