"""A jq filter is not a key file: ``.key`` is refused only when it is a file.

The printing gate (``exec_reads_secret``) matched every argument of a printer
against the key-file name pattern, so ``jq -r .key`` and ``jq '.data.key'``
were refused as "would print a secrets file". A key-file name counts only when
it is spelled as a path (a separator, ``~``, ``$HOME``) or names a file that
exists in the working directory. Exact credential names (``.env``,
``credentials``) are still refused as bare words.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from robothor.engine.secret_paths import exec_reads_secret
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home / "ws"


def _exec(ws: Path, command: str) -> dict[str, Any]:
    ctx = ToolContext(agent_id="main", workspace=str(ws))
    return asyncio.run(HANDLERS["exec"]({"command": command}, ctx))


def _admitted(out: dict[str, Any]) -> None:
    """Both gates let it through: it ran (whatever its exit code)."""
    assert "refused" not in str(out.get("error", "")), out
    assert out.get("error_type") is None, out
    assert "exit_code" in out, out


@pytest.mark.parametrize(
    "command",
    ["echo '{\"key\":1}' | jq -r .key", "jq '.data.key' resp.json", "jq -r .api.pem x.json"],
)
def test_a_jq_filter_passes_both_gates(ws: Path, command: str) -> None:
    _admitted(_exec(ws, command))


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_a_jq_filter_prints_its_value(ws: Path) -> None:
    out = _exec(ws, "echo '{\"key\":1}' | jq -r .key")
    assert out["stdout"].strip() == "1", out


@pytest.mark.parametrize("command", ["jq -r .key", "cat api.key", "head .data.key"])
def test_the_printing_gate_ignores_an_absent_bare_key_name(ws: Path, command: str) -> None:
    assert exec_reads_secret(command, cwd=str(ws)) is None


@pytest.mark.parametrize("command", ["cat ./x.key", "cat x.key"])
def test_an_existing_key_file_is_refused(ws: Path, command: str) -> None:
    (ws / "x.key").write_text("-----BEGIN PRIVATE KEY-----\n")
    out = _exec(ws, command)
    assert "error" in out and "stdout" not in out, out
    assert exec_reads_secret(command, cwd=str(ws)) is not None


@pytest.mark.parametrize("command", ["cat ~/.ssh/id_rsa", "cat $HOME/certs/a.pem", "cat .env"])
def test_explicit_and_exact_names_are_still_refused(ws: Path, command: str) -> None:
    assert exec_reads_secret(command, cwd=str(ws)) is not None
