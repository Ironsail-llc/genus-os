"""``exec`` must not print the vault master key by any spelling.

An end-to-end run found four commands that printed the raw key bytes after
``GENUS_VAULT_KEY_FILE`` moved the key, each past a guard that only looked at
a printer's file arguments:

(a) ``od -An -tx1 $GENUS_VAULT_KEY_FILE`` — the variable was never expanded
    before the check, and the exec environment carried it;
(b) ``xxd < <key>`` — a redirect operand is not a printer argument;
(c) ``dd if=<key> | base64`` — ``dd`` is not a printer;
(d) ``cp <key> ws/k.bin && od ws/k.bin`` — copying first hides the name.

The fix is in layers: the variables never reach the child, a command that
names them is refused, redirect operands and ``if=``/``of=`` are read paths,
copy and archive tools are readers, and — whatever the command — any argument
or literal naming the key file or its directory is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from robothor.engine.secret_paths import exec_reads_secret

if TYPE_CHECKING:
    from pathlib import Path

ENVS = (
    "GENUS_VAULT_KEY_FILE",
    "ROBOTHOR_VAULT_KEY_FILE",
    "GENUS_FINGERPRINT_SALT_FILE",
    "ROBOTHOR_FINGERPRINT_SALT_FILE",
)


@pytest.fixture
def key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "workspace"))
    mount = tmp_path / "genus" / "vault"
    mount.mkdir(parents=True)
    path = mount / ".vault-key"
    path.write_bytes(b"\x00" * 32)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(path))
    return path


# ── (a) the variable ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "od -An -tx1 $GENUS_VAULT_KEY_FILE",
        'cat "${GENUS_VAULT_KEY_FILE}"',
        "base64 $ROBOTHOR_VAULT_KEY_FILE",
        "K=$GENUS_FINGERPRINT_SALT_FILE; wc -c $K",
    ],
)
def test_a_command_naming_the_variable_is_refused(key, command) -> None:
    assert exec_reads_secret(command)


@pytest.mark.parametrize("rung", ["off", "observe", "enforce"])
def test_the_variables_never_reach_the_child(key, rung) -> None:
    from robothor.engine.exec_env import build_exec_env

    base = dict.fromkeys(ENVS, "/etc/genus/vault/x")
    base["PATH"] = "/usr/bin"
    env = build_exec_env(agent_id="agent-a", mode=rung, base=base).env
    assert not set(ENVS) & set(env), env
    assert env.get("PATH") == "/usr/bin"


# ── (b) redirects ────────────────────────────────────────────────────


def test_an_input_redirect_of_the_key_is_refused(key) -> None:
    assert exec_reads_secret(f"xxd < {key} | head -1")
    assert exec_reads_secret(f"base64 <{key}")


def test_an_input_redirect_of_any_secret_file_is_refused(tmp_path) -> None:
    assert exec_reads_secret(f"xxd < {tmp_path}/.ssh/id_rsa")
    assert exec_reads_secret(f"base64 <<< $(<{tmp_path}/.env)")


# ── (c) dd and friends ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "template",
    [
        "dd if={key} 2>/dev/null | base64",
        "openssl base64 -in {key}",
        "uuencode {key} k",
        "tar cf - {key} | base64",
        "rsync {key} /tmp/k",
        "split -b 8 {key} part",
    ],
)
def test_readers_that_are_not_printers_are_refused(key, template) -> None:
    assert exec_reads_secret(template.format(key=key))


def test_dd_of_any_secret_file_is_refused(tmp_path) -> None:
    assert exec_reads_secret(f"dd if={tmp_path}/.env | base64")


# ── (d) copy first ───────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["cp", "mv", "install", "ln -s"])
def test_copying_the_key_is_refused(key, verb) -> None:
    assert exec_reads_secret(f"{verb} {key} ws/k.bin && od ws/k.bin")


def test_anything_naming_the_key_directory_is_refused(key) -> None:
    assert exec_reads_secret(f"ls -la {key.parent} && sha256sum {key.parent}/*")
    assert exec_reads_secret(f"find {key.parent} -type f -exec od {{}} +")


# ── interpreters ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template",
    [
        "python -c \"open('{key}').read()\"",
        "python3 -c \"print(open('{key}','rb').read().hex())\"",
        "node -e \"console.log(require('fs').readFileSync('{key}'))\"",
        "perl -e 'open F, \"<{key}\"; print <F>'",
    ],
)
def test_a_one_liner_that_mentions_the_key_is_refused(key, template) -> None:
    assert exec_reads_secret(template.format(key=key))


def test_a_one_liner_reading_a_secret_file_by_name_is_refused() -> None:
    assert exec_reads_secret("python3 -c \"print(open('.env').read())\"")


# ── what stays allowed ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cp notes.txt backup.txt",
        "dd if=/dev/zero of=/tmp/blank bs=1 count=8",
        "python3 -c \"print(open('README.md').read())\"",
        "xxd < image.png | head",
        "echo $ROBOTHOR_WORKSPACE",
    ],
)
def test_ordinary_commands_still_run(key, command) -> None:
    assert exec_reads_secret(command) is None
