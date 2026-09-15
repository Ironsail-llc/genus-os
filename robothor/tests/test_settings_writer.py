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

from robothor.settings.sources import write_setting, write_settings


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
    patching the writer and checking the command went through it.

    Patched at ``robothor.settings.operator``, which is where the CLI's write
    path now lives — the same module ``PATCH /api/settings`` calls, so this
    also pins that neither surface can route around the shared writer.
    """
    import argparse

    from robothor.cli import config_cmd

    calls: list[tuple] = []
    monkeypatch.setattr(
        "robothor.settings.operator.write_settings",
        lambda *a, **k: (calls.append((a, k)), tmp_path / "config.yaml")[1],
    )
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))

    rc = config_cmd.cmd_config(
        argparse.Namespace(config_command="set", name="GENUS_LOCAL_LOGIN", value="true", json=False)
    )

    assert rc == 0
    assert calls, "genus config set bypassed the shared settings writer"
    # One change, spelled (group, field, value, other spellings).
    assert calls[0][0][0] == [("auth", "local_login", True, ("GENUS_LOCAL_LOGIN",))]


# ── C1: every scalar must round-trip through the file ────────────────────────

#: Values an operator can type into the Config form. Each one is a string the
#: field accepts, so ``validate()`` passes it through -- the corruption, if any,
#: happens strictly inside the writer.
#:
#: ``- item`` is the one that actually broke: a YAML block-sequence indicator is
#: ``-`` followed by a space, and the old hand-rolled quoting rule did not list
#: ``-``. It wrote the value bare, the file stopped parsing, and the page an
#: operator would use to undo it started returning 500.
ROUND_TRIP_CASES = [
    "- item",
    "- ",
    "? x",
    ": x",
    "yes",
    "no",
    "on",
    "off",
    "null",
    "~",
    "true",
    "1e3",
    "0644",
    "3",
    "3.5",
    "#comment",
    "@at",
    "&anchor",
    "*alias",
    "!!python/object/apply:os.system",
    "{a: 1}",
    "[1, 2]",
    "a: b",
    "value\nmax_concurrent_agents: 999",
    "  leading and trailing  ",
    "",
    "/var/log/robothor",
    "plain",
]


@pytest.mark.parametrize("value", ROUND_TRIP_CASES)
def test_every_scalar_round_trips_as_the_same_string(config_path: Path, value: str) -> None:
    """What was written is what the instance reads back. No exceptions.

    The rule cannot be a list of characters to quote -- that list was wrong
    once and would be wrong again. The writer emits through the same YAML
    dumper the platform loads with, so the two cannot disagree.
    """
    write_setting("paths", "log_dir", value, path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["settings"]["paths"]["log_dir"] == value, text


def test_a_value_that_would_not_round_trip_is_refused_rather_than_written(
    config_path: Path, monkeypatch
) -> None:
    """The guard is the round trip itself, not an enumeration of bad inputs.

    If a future dumper change (or a value nobody imagined) produced a file that
    reads back as something else, the operator gets an error and the file they
    had -- never a 200 and an instance that will not start.
    """
    config_path.write_text("settings:\n  paths:\n    log_dir: /old\n", encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")

    # A renderer that writes a block-sequence indicator bare: exactly the bug.
    monkeypatch.setattr("robothor.settings.config_file._render", lambda _value: "- item")
    with pytest.raises(OSError):
        write_setting("paths", "log_dir", "- item", path=config_path)

    assert config_path.read_text(encoding="utf-8") == original


# ── C2: concurrent writers must not lose each other's changes ────────────────


def test_two_concurrent_writers_both_land(config_path: Path) -> None:
    """Read-splice-replace with no lock is a lost update that reports success.

    The bridge's handlers are plain ``def``, so FastAPI runs them in its worker
    threadpool -- genuinely parallel. The barrier below forces the interleaving
    a threadpool produces on its own: both threads read the file before either
    replaces it. Without a lock the second replace discards the first key and
    both callers are told the change applied.
    """
    import threading

    config_path.write_text("settings:\n", encoding="utf-8")
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _write(group: str, field: str, value: object) -> None:
        try:
            start.wait(timeout=5)
            write_setting(group, field, value, path=config_path)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=_write, args=("paths", "log_dir", "/var/log/race")),
        threading.Thread(target=_write, args=("engine", "max_concurrent_agents", 8)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    block = _block(config_path)
    assert block["paths"]["log_dir"] == "/var/log/race"
    assert block["engine"]["max_concurrent_agents"] == 8


# ── I3: a batch is one splice and one replace ────────────────────────────────


def test_a_batch_of_settings_is_written_in_one_replace(config_path: Path, monkeypatch) -> None:
    """Per-field ``os.replace`` makes a batch atomic per FIELD, not per request:
    a failure on the second field leaves the first one written."""
    replaces: list[object] = []
    real_replace = Path.replace

    def _counting_replace(self, target):
        replaces.append(target)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _counting_replace)
    write_settings(
        [
            ("paths", "log_dir", "/var/log/batch", ()),
            ("engine", "max_concurrent_agents", 8, ()),
        ],
        path=config_path,
    )

    assert len(replaces) == 1, "a batch must be one atomic replace, not one per field"
    block = _block(config_path)
    assert block["paths"]["log_dir"] == "/var/log/batch"
    assert block["engine"]["max_concurrent_agents"] == 8


def test_a_failed_batch_leaves_the_file_byte_identical(config_path: Path, monkeypatch) -> None:
    config_path.write_text("settings:\n  paths:\n    log_dir: /old\n", encoding="utf-8")
    original = config_path.read_bytes()

    def _boom(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError):
        write_settings(
            [
                ("paths", "log_dir", "/var/log/one", ()),
                ("engine", "max_concurrent_agents", 8, ()),
            ],
            path=config_path,
        )

    assert config_path.read_bytes() == original
    assert sorted(p.name for p in config_path.parent.iterdir()) == ["config.yaml"]
