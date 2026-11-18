"""``read_file`` on a directory answers with the listing, not an error.

A shipped instruction told the operator-facing agent to ``read_file`` a
directory of manifests, and the call failed with "Is a directory" on every
turn. A directory now returns a successful result: its entries and a note
that it is a directory, so the agent can pick a file and read that.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robothor.engine.tools.handlers import filesystem


async def read_file(path, workspace):
    ctx = MagicMock()
    ctx.workspace = str(workspace)
    return await filesystem.HANDLERS["read_file"]({"path": str(path)}, ctx)


@pytest.mark.asyncio
async def test_directory_returns_listing_and_note(tmp_path) -> None:
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "alice.yaml").write_text("id: alice\n")
    (agents / "bob.yaml").write_text("id: bob\n")
    (agents / "archive").mkdir()

    out = await read_file("docs/agents", tmp_path)

    assert "error" not in out
    assert out["is_directory"] is True
    names = {e["name"]: e["type"] for e in out["entries"]}
    assert names == {"alice.yaml": "file", "bob.yaml": "file", "archive": "dir"}
    assert "directory" in out["note"].lower()
    assert "read_file" in out["note"]


@pytest.mark.asyncio
async def test_directory_with_trailing_slash(tmp_path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("x")

    out = await read_file("notes/", tmp_path)

    assert out.get("is_directory") is True
    assert [e["name"] for e in out["entries"]] == ["a.md"]


@pytest.mark.asyncio
async def test_missing_path_is_still_an_error(tmp_path) -> None:
    out = await read_file("nope", tmp_path)
    assert "error" in out


@pytest.mark.asyncio
async def test_a_file_reads_as_before(tmp_path) -> None:
    (tmp_path / "a.md").write_text("hello")
    out = await read_file("a.md", tmp_path)
    assert out["content"] == "hello"
    assert "is_directory" not in out
