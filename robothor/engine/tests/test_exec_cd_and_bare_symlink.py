"""A bare word is judged in the directory the shell will read it from.

``exec`` runs the whole command string in a shell, starting in the workspace.
Two gates looked at a bare word (no separator, no ``~``) only as the workspace
would see it:

* the printing gate exempts a key-file-shaped word that names no file (a jq
  filter such as ``.key``), but checked existence in the workspace even when the
  command first changes directory: ``cd ~ && cat server.key`` printed a key;
* the credential_path gate never resolved a plain filename, so a workspace
  symlink onto a credential (``cat innocent.txt``) printed what it points at.

A ``cd``/``pushd`` (or ``-C`` on git, make or tar) to a literal directory moves
where a bare word is looked up; to anything else, a bare key-file word is no
longer exempt. A final ``**`` matches files as well as directories.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS
from robothor.identity import IdentityContext

if TYPE_CHECKING:
    from pathlib import Path

FAKE = "not-a-real-key"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def _top_level_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from robothor.engine import session_registry

    monkeypatch.setattr(
        session_registry,
        "lookup",
        lambda run_id: SimpleNamespace(run=SimpleNamespace(parent_run_id=None)),
    )


def _exec(home: Path, command: str) -> dict[str, Any]:
    identity = IdentityContext(
        tenant_id=DEFAULT_TENANT,
        channel="telegram",
        identifier="owner",
        verified=True,
        role="owner",
    )
    ctx = ToolContext(agent_id="main", workspace=str(home / "ws"), identity=identity)
    return asyncio.run(HANDLERS["exec"]({"command": command}, ctx))


def _write(path: Path, text: str = FAKE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    return path


def _refused(out: dict[str, Any]) -> None:
    assert "refused" in str(out.get("error", "")), out
    assert "exit_code" not in out, out
    assert FAKE not in str(out), out


def _admitted(out: dict[str, Any]) -> None:
    assert "refused" not in str(out.get("error", "")), out
    assert out.get("error_type") is None, out
    assert "exit_code" in out, out


# ── 1. a directory change moves where a bare key-file word is looked up ──────


@pytest.mark.parametrize(
    "command",
    [
        "cd ~ && cat server.key",
        "cd $HOME; cat server.key",
        "pushd ~ >/dev/null && cat server.key",
        "cd && cat server.key",
    ],
)
def test_a_cd_home_then_a_bare_key_file_is_refused(home: Path, command: str) -> None:
    _write(home / "server.key")
    _refused(_exec(home, command))


def test_a_cd_to_a_literal_directory_then_its_key_file_is_refused(
    home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "x.key")
    _refused(_exec(home, f"cd {elsewhere} && cat x.key"))


def test_a_relative_cd_then_its_key_file_is_refused(home: Path) -> None:
    _write(home / "ws" / "certs" / "client.p12")
    _refused(_exec(home, "cd certs && base64 client.p12"))


@pytest.mark.parametrize(
    "command",
    ['cd "$KEYDIR" && cat x.key', "cd - && cat x.key", "popd; cat x.key", "cd $(pwd) && cat x.key"],
)
def test_a_cd_to_an_unknown_directory_keeps_the_refusal(home: Path, command: str) -> None:
    _refused(_exec(home, command))


def test_a_dash_c_directory_option_is_followed(home: Path) -> None:
    _write(home / "server.key")
    _refused(_exec(home, "tar -C ~ -cf - server.key | tar -xOf -"))


@pytest.mark.parametrize(
    "command",
    [
        "echo '{\"key\":1}' | jq -r .key",
        "jq '.data.key' resp.json",
        "cd sub && jq -r .key resp.json",
        "grep -C 2 key notes.txt",
    ],
)
def test_a_jq_filter_still_passes(home: Path, command: str) -> None:
    (home / "ws" / "sub").mkdir()
    _write(home / "ws" / "notes.txt", "a key line")
    _admitted(_exec(home, command))


# ── 2. a bare filename that is a symlink onto a credential ────────────────────


def test_a_bare_workspace_symlink_onto_a_credential_is_refused(home: Path) -> None:
    key = _write(home / ".ssh" / "id_rsa")
    (home / "ws" / "innocent.txt").symlink_to(key)
    out = _exec(home, "cat innocent.txt")
    assert out.get("error_type") == "credential_path", out
    _refused(out)


def test_a_bare_symlink_after_a_literal_cd_is_refused(home: Path) -> None:
    key = _write(home / ".aws" / "credentials")
    (home / "ws" / "sub").mkdir()
    (home / "ws" / "sub" / "inner.txt").symlink_to(key)
    out = _exec(home, "cd sub && cat inner.txt")
    assert out.get("error_type") == "credential_path", out
    _refused(out)


def test_a_bare_regular_file_is_read(home: Path) -> None:
    _write(home / "ws" / "notes.txt", "hello notes")
    out = _exec(home, "cat notes.txt")
    _admitted(out)
    assert out["stdout"].strip() == "hello notes", out


def test_a_bare_word_naming_nothing_is_unaffected(home: Path) -> None:
    out = _exec(home, "cat nothing-here.txt")
    _admitted(out)
    assert out["exit_code"] != 0, out


# ── 3. a final ** matches files too ───────────────────────────────────────────


def test_a_final_double_star_reaches_a_linked_key(home: Path) -> None:
    key = _write(home / ".ssh" / "id_rsa")
    (home / "ws" / "link").symlink_to(key)
    out = _exec(home, "cat **")
    assert out.get("error_type") == "credential_path", out
    _refused(out)


def test_a_final_double_star_under_a_listed_directory_is_refused(home: Path) -> None:
    _write(home / ".ssh" / "id_rsa")
    out = _exec(home, "cat ~/.ssh/**")
    assert out.get("error_type") == "credential_path", out
    _refused(out)
