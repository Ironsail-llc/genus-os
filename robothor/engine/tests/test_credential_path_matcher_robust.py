"""credential_path's matcher never crashes, never blows up, never misfires.

Review findings on the resolving matcher: a ``~word`` that is not a home
spelling crashed ``exec`` and ``execute_code``; brace expansion was exponential
and a JSON body reached the glob walk; a bare word such as ``.key`` was joined
onto the workspace and refused as a key file under home; the glob walk ran on
the event loop with no bound on entries visited; and the root check was a bare
string prefix, so a sibling directory counted as inside the workspace.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from robothor.engine import secret_paths
from robothor.engine.secret_paths import (
    code_names_credential,
    credential_roots,
    exec_names_credential,
)
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _roots(home: Path) -> tuple[str, ...]:
    return credential_roots(home / "ws")


def _exec(home: Path, command: str) -> dict[str, Any]:
    ctx = ToolContext(agent_id="main", workspace=str(home / "ws"))
    return asyncio.run(HANDLERS["exec"]({"command": command}, ctx))


# ── (1) a ~word that is not a home spelling ───────────────────────────


@pytest.mark.parametrize(
    "command",
    ['git commit -m "~40% faster"', "git show @~1", "echo HEAD~2 x=~3 (~4)"],
)
def test_a_tilde_word_that_is_not_a_home_never_raises(home: Path, command: str) -> None:
    assert exec_names_credential(command, roots=_roots(home)) is None


def test_a_tilde_word_runs_through_exec(home: Path) -> None:
    out = _exec(home, 'echo "~40% faster"')
    assert out.get("exit_code") == 0, out
    assert "~40% faster" in out["stdout"]


def test_a_snippet_with_a_bitwise_not_is_not_refused() -> None:
    assert code_names_credential("flags = 3\nmask = ~flags\nprint(mask)") is None


def test_an_unknown_user_home_is_not_a_path() -> None:
    assert not secret_paths.is_credential_path("~nosuchuser-xyz/.ssh-not")


# ── (2) brace and JSON words do not blow up ──────────────────────────


def test_a_json_body_is_fast_and_not_refused(home: Path) -> None:
    records = ",".join(f'{{"id":{i},"name":"n{i}","tags":["a","b"]}}' for i in range(20))
    command = f"curl -s -d '[{records}]' https://api.example.com/v1/items"
    started = time.perf_counter()
    assert exec_names_credential(command, roots=_roots(home)) is None
    assert time.perf_counter() - started < 0.5


def test_a_brace_bomb_is_bounded(home: Path) -> None:
    command = "echo " + "{a,b}" * 24 + "/x"
    started = time.perf_counter()
    exec_names_credential(command, roots=_roots(home))
    assert time.perf_counter() - started < 0.5


def test_a_small_brace_still_expands_onto_a_listed_path(home: Path) -> None:
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("x")
    link = home / "ws" / "b"
    link.symlink_to(home / ".ssh" / "id_rsa")
    out = exec_names_credential("cat {a,b}", roots=_roots(home))
    assert out is not None and out["error_type"] == "credential_path"


# ── (3) bare words are not paths ──────────────────────────────────────


@pytest.mark.parametrize("command", ["jq -r .key", "jq '.data.key' resp.json", "echo api.key"])
def test_a_bare_word_is_not_a_key_under_home(home: Path, command: str) -> None:
    assert exec_names_credential(command, roots=_roots(home)) is None


def test_an_existing_key_file_under_home_is_refused(home: Path) -> None:
    (home / "ws" / "id_rsa.key").write_text("x")
    out = exec_names_credential("cat ./id_rsa.key", roots=_roots(home))
    assert out is not None and out["error_type"] == "credential_path"


# ── (4) the walk is bounded and runs off the event loop ──────────────


def test_the_glob_walk_is_bounded_by_entries_visited(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = home / "ws" / "many"
    many.mkdir()
    for i in range(50):
        (many / f"f{i}.txt").write_text("x")
    visited: list[str] = []
    real = secret_paths._realpath_is_credential

    def _counting(spelled: str) -> bool:
        visited.append(spelled)
        return real(spelled)

    monkeypatch.setattr(secret_paths, "_GLOB_WALK_CAP", 5)
    monkeypatch.setattr(secret_paths, "_realpath_is_credential", _counting)
    exec_names_credential("cat many/*.txt", roots=_roots(home))
    assert len(visited) <= 5


def test_exec_runs_the_matcher_off_the_event_loop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def _record(command: str, **_kw: Any) -> None:
        seen.append(threading.current_thread() is threading.main_thread())

    monkeypatch.setattr(secret_paths, "exec_names_credential", _record)
    _exec(home, "echo ok")
    assert seen == [False], "the matcher must not run on the loop's thread"


# ── (5) a sibling directory is not inside a root ─────────────────────


def test_a_sibling_of_the_workspace_is_not_expanded(home: Path) -> None:
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("x")
    sibling = home / "wsx"
    sibling.mkdir()
    (sibling / "link").symlink_to(home / ".ssh" / "id_rsa")
    roots = (str(home / "ws"),)  # the workspace only, not the home directory
    assert exec_names_credential(f"cat {sibling}/l*", roots=roots) is None
