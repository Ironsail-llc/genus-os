"""``manifests.unrestricted_role``: which agents run with no role restriction.

An agent whose manifest declares no role resolves to the default service role,
which the shipped permissions seed as allow-all, and nothing reported it. The
check lists every such agent, as the engine resolves it (fleet defaults and
the default-role setting included), as a ``recommended`` failure: a warning,
not a broken instance. It changes nothing about how any agent runs.
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
    "instruction_file": "example.md",
}


@pytest.fixture
def manifest_dir(monkeypatch, tmp_path) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(manifest_checks, "_manifest_dir", lambda _ctx: directory)
    monkeypatch.delenv("ROBOTHOR_DEFAULT_SERVICE_ROLE", raising=False)
    return directory


def _write(directory: Path, agent_id: str, **extra: object) -> None:
    (directory / f"{agent_id}.yaml").write_text(yaml.safe_dump({"id": agent_id, **_BASE, **extra}))


def _run():
    check = next(c for c in manifest_checks.CHECKS if c.id == "manifests.unrestricted_role")
    answer = asyncio.run(check.run(make_ctx()))
    return check, (answer if isinstance(answer, list) else [answer])


def test_the_check_is_a_warning(manifest_dir) -> None:
    check, _rows = _run()
    assert check.severity == "recommended"
    assert check.category == "manifests"


def test_every_roleless_agent_is_listed(manifest_dir) -> None:
    _write(manifest_dir, "alice")
    _write(manifest_dir, "bob")
    _write(manifest_dir, "carol", role="viewer")
    _check, rows = _run()
    (row,) = rows
    assert row.status == "fail"
    assert "alice" in row.detail and "bob" in row.detail
    assert "carol" not in row.detail
    assert "role:" in row.detail


def test_a_fleet_whose_agents_all_declare_a_role_passes(manifest_dir) -> None:
    _write(manifest_dir, "carol", role="viewer")
    _check, rows = _run()
    assert [r.status for r in rows] == ["pass"]


def test_the_default_role_setting_is_honored(manifest_dir, monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_DEFAULT_SERVICE_ROLE", "viewer")
    _write(manifest_dir, "alice")
    _check, rows = _run()
    assert [r.status for r in rows] == ["pass"]


def test_no_manifest_directory_skips(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(manifest_checks, "_manifest_dir", lambda _ctx: tmp_path / "missing")
    _check, rows = _run()
    assert [r.status for r in rows] == ["skip"]
