"""credential_path, resolved: a symlink or a glob cannot reach a listed path.

The named-path check refuses a command or a read that SPELLS a listed path.
It does not, on its own, refuse a symlink that points at one, nor a glob or a
command substitution that expands to one. These tests pin those down: read_file resolves every path component's realpath before the
check, and exec refuses a word whose glob expansion (bounded, under the home
directory and the workspace) hits a listed path, a word that is a symlink into
one, and a substitution or find/xargs pipeline that names a listed directory.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS
from robothor.identity import IdentityContext

if TYPE_CHECKING:
    from pathlib import Path

CANARY = "canary-" + "Zr8qLm2Vx7Tn4Kp9"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _secret(home: Path, rel: str) -> Path:
    path = home / rel.removeprefix("~/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"SECRET={CANARY}\n")
    return path


def _owner_ctx(home: Path) -> ToolContext:
    identity = IdentityContext(
        tenant_id=DEFAULT_TENANT,
        channel="telegram",
        identifier="owner",
        verified=True,
        role="owner",
    )
    return ToolContext(agent_id="main", workspace=str(home / "ws"), identity=identity)


def _assert_gate(out: dict[str, Any]) -> None:
    assert out.get("error_type") == "credential_path", out
    assert CANARY not in str(out)


def _read(home: Path, path: str) -> dict[str, Any]:
    return asyncio.run(HANDLERS["read_file"]({"path": path}, _owner_ctx(home)))


def _exec(home: Path, command: str) -> dict[str, Any]:
    return asyncio.run(HANDLERS["exec"]({"command": command}, _owner_ctx(home)))


@pytest.fixture(autouse=True)
def _top_level_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from robothor.engine import session_registry

    monkeypatch.setattr(
        session_registry,
        "lookup",
        lambda run_id: SimpleNamespace(run=SimpleNamespace(parent_run_id=None)),
    )


# ── read_file through a symlink ──────────────────────────────────────


def test_read_file_refuses_a_symlink_to_a_listed_file(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    link = home / "ws" / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_rsa")
    _assert_gate(_read(home, str(link)))


def test_read_file_refuses_a_file_under_a_symlinked_directory(home: Path) -> None:
    _secret(home, "~/.aws/credentials")
    link_dir = home / "ws" / "data"
    link_dir.symlink_to(home / ".aws")
    _assert_gate(_read(home, str(link_dir / "credentials")))


def test_read_file_follows_a_plain_file_through_a_symlink(home: Path) -> None:
    target = _secret(home, "ws/notes.txt")
    link = home / "ws" / "link.txt"
    link.symlink_to(target)
    out = _read(home, str(link))
    assert CANARY in out.get("content", ""), out


# ── exec: a symlinked word ───────────────────────────────────────────


def test_exec_refuses_cat_of_a_symlink_into_a_listed_path(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    link = home / "ws" / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_rsa")
    _assert_gate(_exec(home, f"cat {link}"))


# ── exec: a glob that expands onto a listed path ─────────────────────


def test_exec_refuses_a_glob_over_the_ssh_directory(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    _assert_gate(_exec(home, "cat ~/.ssh/*"))


def test_exec_refuses_a_glob_over_a_workspace_cli_token(home: Path) -> None:
    _secret(home, "~/.config/gws-cli/token.json")
    _assert_gate(_exec(home, "cat ~/.config/gws*/token*.json"))


def test_exec_refuses_a_recursive_glob_that_reaches_a_listed_path(home: Path) -> None:
    _secret(home, "~/.config/gcloud/adc.json")
    _assert_gate(_exec(home, "cat ~/.config/**/*.json"))


def test_exec_allows_a_glob_that_matches_only_ordinary_files(home: Path) -> None:
    # cwd is the workspace, so the glob is relative to it.
    _secret(home, "ws/a.txt")
    _secret(home, "ws/b.txt")
    out = _exec(home, "cat *.txt")
    assert out.get("exit_code") == 0, out
    assert CANARY in out["stdout"]


# ── exec: command substitution and find/xargs indirection ────────────


def test_exec_refuses_a_find_substitution_naming_a_listed_dir(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    _assert_gate(_exec(home, "cat $(find ~/.ssh -name '*')"))


def test_exec_refuses_a_find_exec_over_a_listed_dir(home: Path) -> None:
    _secret(home, "~/.aws/credentials")
    _assert_gate(_exec(home, "find ~/.aws -type f -exec cat {} +"))


def test_exec_refuses_a_backtick_substitution_naming_a_listed_dir(home: Path) -> None:
    _secret(home, "~/.config/gcloud/adc.json")
    _assert_gate(_exec(home, "cat `ls ~/.config/gcloud`"))


def test_exec_refuses_xargs_over_a_listed_dir(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    _assert_gate(_exec(home, "ls ~/.ssh | xargs cat"))


# ── execute_code: the proxy's read_file sees the same realpath rule ──


def test_a_snippet_reading_a_symlink_is_refused_by_read_file(home: Path) -> None:
    _secret(home, "~/.ssh/id_rsa")
    link = home / "ws" / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_rsa")
    # The snippet's own read_file goes through the same handler.
    _assert_gate(_read(home, os.fspath(link)))
