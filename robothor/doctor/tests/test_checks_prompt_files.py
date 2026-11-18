"""``manifests.prompt_files``: every instruction and bootstrap file exists.

The engine skips a manifest's missing instruction or bootstrap file and keeps
going, so an agent with a stale path runs without the text it was meant to
have and nothing visible says so. This check names each agent and missing
path, and fails.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.doctor.checks import manifests as manifest_checks
from robothor.doctor.tests.conftest import make_ctx

if TYPE_CHECKING:
    from pathlib import Path

_BASE = {
    "name": "Example",
    "description": "A generic fixture agent.",
    "version": "2026-01-01",
    "department": "examples",
}


@pytest.fixture
def manifest_dir(monkeypatch, tmp_path) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(manifest_checks, "_manifest_dir", lambda _ctx: directory)
    return directory


def _write(directory: Path, agent_id: str, **extra: object) -> None:
    (directory / f"{agent_id}.yaml").write_text(yaml.safe_dump({"id": agent_id, **_BASE, **extra}))


def _run():
    check = next(c for c in manifest_checks.CHECKS if c.id == "manifests.prompt_files")
    ctx = make_ctx()
    answer = asyncio.run(check.run(ctx))
    return check, ctx, (answer if isinstance(answer, list) else [answer])


def test_the_check_is_required() -> None:
    check = next(c for c in manifest_checks.CHECKS if c.id == "manifests.prompt_files")
    assert check.severity == "required"
    assert check.category == "manifests"


def test_missing_files_are_named_with_their_agent(manifest_dir) -> None:
    _write(
        manifest_dir,
        "alice",
        instruction_file="brain/agents/ALICE.md",
        bootstrap_files=["brain/SOUL.md", "brain/MEMORY.md"],
    )
    workspace = make_ctx().workspace
    (workspace / "brain").mkdir(parents=True, exist_ok=True)
    (workspace / "brain" / "SOUL.md").write_text("soul")

    _check, _ctx, rows = _run()

    (row,) = rows
    assert row.status == "fail"
    assert "alice: brain/agents/ALICE.md" in row.detail
    assert "alice: brain/MEMORY.md" in row.detail
    assert "SOUL.md" not in row.detail


def test_every_file_present_passes(manifest_dir) -> None:
    _write(manifest_dir, "bob", instruction_file="brain/agents/BOB.md", bootstrap_files=[])
    workspace = make_ctx().workspace
    (workspace / "brain" / "agents").mkdir(parents=True, exist_ok=True)
    (workspace / "brain" / "agents" / "BOB.md").write_text("bob")

    _check, _ctx, rows = _run()

    assert [r.status for r in rows] == ["pass"]


def test_no_manifest_directory_skips(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(manifest_checks, "_manifest_dir", lambda _ctx: tmp_path / "nope")
    _check, _ctx, rows = _run()
    assert [r.status for r in rows] == ["skip"]
