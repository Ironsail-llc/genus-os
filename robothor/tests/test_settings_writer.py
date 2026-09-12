"""One writer for ``config.yaml``'s ``settings:`` block.

Two callers need to store a typed setting in the file the instance reads:
``genus config set``, and the first-run wizard's operator step, which has to
turn ``GENUS_LOCAL_LOGIN`` on so the account it just created can sign in.

A second implementation would have been a second opinion about what that file
means — indentation, an existing key under a deprecated spelling, an inline
comment, the atomic replace, the file mode — and the failure mode is a
dashboard that reports a setting applied while the service reads something
else. So the machinery lives in ``robothor.settings.sources`` and the CLI is
one of its callers.

These tests cover the writer directly. The CLI's own behaviour is unchanged
and stays covered by ``robothor/cli/tests/test_config_cmd.py``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path  # noqa: TC003 - used in runtime annotations below

import pytest
import yaml

from robothor.settings.sources import write_setting


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def _block(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("settings") or {}


def test_creates_the_file_and_the_block(config_path: Path) -> None:
    write_setting("auth", "local_login", True, path=config_path)

    assert _block(config_path)["auth"]["local_login"] is True


def test_renders_a_bool_as_yaml_not_python(config_path: Path) -> None:
    write_setting("auth", "local_login", True, path=config_path)

    assert "local_login: true" in config_path.read_text(encoding="utf-8")


def test_a_new_file_is_private_to_the_operator(config_path: Path) -> None:
    write_setting("auth", "local_login", True, path=config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_preserves_unrelated_keys_and_comments(config_path: Path) -> None:
    config_path.write_text(
        "# hand-written, and it stays that way\n"
        "settings:\n"
        "  database:\n"
        "    name: robothor_memory  # the live one\n"
        "  auth:\n"
        "    auth_enforce: true\n",
        encoding="utf-8",
    )

    write_setting("auth", "local_login", True, path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "# hand-written, and it stays that way" in text
    assert "name: robothor_memory  # the live one" in text
    assert "auth_enforce: true" in text
    assert _block(config_path)["auth"]["local_login"] is True


def test_updates_an_existing_value_in_place(config_path: Path) -> None:
    config_path.write_text(
        "settings:\n  auth:\n    local_login: false  # not yet\n", encoding="utf-8"
    )

    write_setting("auth", "local_login", True, path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "local_login: true  # not yet" in text
    assert text.count("local_login") == 1


def test_updates_a_key_written_under_its_environment_spelling(config_path: Path) -> None:
    """Groups are ``populate_by_name``, so the operator's own spelling is legal
    and must be updated rather than joined by a second key — a mapping with the
    field twice means the file's meaning depends on read order."""
    config_path.write_text("settings:\n  auth:\n    GENUS_LOCAL_LOGIN: false\n", encoding="utf-8")

    write_setting("auth", "local_login", True, names=("GENUS_LOCAL_LOGIN",), path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "GENUS_LOCAL_LOGIN: true" in text
    assert "local_login: true" not in text


def test_keeps_the_mode_of_an_existing_file(config_path: Path) -> None:
    config_path.write_text("settings: {}\n", encoding="utf-8")
    config_path.chmod(0o640)

    write_setting("auth", "local_login", True, path=config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_leaves_no_temporary_file_behind(config_path: Path) -> None:
    write_setting("auth", "local_login", True, path=config_path)

    assert sorted(p.name for p in config_path.parent.iterdir()) == ["config.yaml"]


def test_a_failed_write_leaves_the_original_intact(config_path: Path, monkeypatch) -> None:
    config_path.write_text("settings:\n  auth:\n    local_login: false\n", encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")

    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_setting("auth", "local_login", True, path=config_path)

    assert config_path.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in config_path.parent.iterdir()) == ["config.yaml"]


def test_returns_the_path_it_wrote(config_path: Path) -> None:
    assert write_setting("auth", "local_login", True, path=config_path) == config_path


def test_the_cli_uses_this_writer(tmp_path, monkeypatch) -> None:
    """`genus config set` must not grow a second implementation. Asserted by
    patching the writer and checking the command went through it."""
    import argparse

    from robothor.cli import config_cmd

    calls: list[tuple] = []
    monkeypatch.setattr(
        config_cmd,
        "write_setting",
        lambda *a, **k: (calls.append((a, k)), tmp_path / "config.yaml")[1],
    )
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))

    rc = config_cmd.cmd_config(
        argparse.Namespace(config_command="set", name="GENUS_LOCAL_LOGIN", value="true", json=False)
    )

    assert rc == 0
    assert calls, "genus config set bypassed the shared settings writer"
    assert calls[0][0][:2] == ("auth", "local_login")
