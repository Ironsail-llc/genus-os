"""The exec guard judges a tool by that tool's own option grammar.

Round-five review findings on the guard (GEN-62). Three regressions and a
minor, all from treating every tool alike:

* ``rsync`` has no ``-t DIR`` target and its ``t``/``S``/``m``/``o``/``g`` are
  plain switches, so a shared copy-tool table made ``rsync -rt .env out``
  swallow the source as an option value.
* a bracket or glob in PROGRAM TEXT (``python3 -c "…['x']"``,
  ``sed -e 's/[0-9]//g' f``, ``grep -e '[A-Z]' f``) is not filename expansion:
  the shell never globs a quoted word, and an interpreter's program argument is
  checked by its string literals instead.
* ``sed``/``awk``/``jq`` take their program through options the guard did not
  know (``--expression=p``, ``--source=…``, ``--from-file=…``), so the first
  positional was taken for the program and the real file skipped every check.
* GNU long options may be abbreviated (``--back``, ``--su``), and a backup
  option renaming a RELATIVE key override was not refused.
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
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory holding one secrets file and ordinary files."""
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("TOKEN=x\n")
    (ws / "notes.txt").write_text("n\n")
    (ws / "config.yaml").write_text("a: 1\n")
    (ws / "data.json").write_text("{}\n")
    (ws / "f").write_text("line\n")
    (ws / "log").write_text("1\n")
    (ws / "prog.jq").write_text(".\n")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(ws))
    monkeypatch.chdir(ws)
    return ws


# -- 1. per-tool option tables ---------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rsync -rt .env out.txt && cat out.txt",
        "rsync -aS .env k",
        "rsync -ao .env k",
        "rsync -am .env k",
        "rsync -avt .env k && od k",
        "scp -P 2222 .env host:/tmp/",
    ],
)
def test_rsync_and_scp_use_their_own_option_grammar(workspace, command) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize(
    "command",
    [
        "rsync -rt src/ out/",
        "rsync -e ssh notes.txt host:/tmp/",
        "cp -rt build/ src/",
        "install -m 644 notes.txt build/",
    ],
)
def test_ordinary_copy_spellings_still_run(workspace, command) -> None:
    assert exec_reads_secret(command) is None, command


# -- 2. program text is not a glob -----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        """python3 -c "import json,sys; print(json.load(sys.stdin)['x'])" """,
        """node -e "console.log(process.argv[1])" """,
        "perl -ne 'print if /[0-9]/' log",
        "sed -e 's/[0-9]//g' f",
        "grep -e '[A-Z]' f",
        "rg -e '\\[ERROR\\]' .",
        "jq -e '.items[0]' data.json",
        "sed 's/[a-z]*//p' notes.txt",
        "awk '{print $1}' f",
        "jq '.[0]' data.json",
    ],
)
def test_program_text_with_brackets_runs(workspace, command) -> None:
    assert exec_reads_secret(command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "cat [[:alpha:]]obothor.env",
        "cat [^x].env",
        "cat .en[v]",
        "cat .en?",
        "cat .env*",
    ],
)
def test_filename_brackets_and_globs_are_still_refused(workspace, command) -> None:
    assert exec_reads_secret(command), command


# -- 3. a program given by option makes every positional a file ------------


@pytest.mark.parametrize(
    "command",
    [
        "sed --expression=p .en?",
        "sed --expr=p .env",
        "sed -n --expression p .env",
        "jq --from-file=prog.jq .en?",
        "jq -f prog.jq .env",
        "gawk --source='{print}' .en?",
        "awk -f prog.jq .env",
    ],
)
def test_a_program_supplied_by_option_leaves_the_file_checked(workspace, command) -> None:
    assert exec_reads_secret(command), command


def test_a_program_supplied_by_option_still_runs_on_ordinary_files(workspace) -> None:
    assert exec_reads_secret("sed --expression=p notes.txt") is None
    assert exec_reads_secret("jq --from-file=prog.jq data.json") is None


# -- 4. backup options: abbreviations and the relative key override --------


@pytest.mark.parametrize(
    "command",
    [
        "cp --back --suf=.txt notes.txt .env && cat .env.txt",
        "cp --backup notes.txt .env",
        "mv --su=.bak notes.txt .env",
        "cp --target /tmp .env",
    ],
)
def test_abbreviated_long_options_are_recognised(workspace, command) -> None:
    assert exec_reads_secret(command), command


def test_a_backup_onto_a_relative_key_override_is_refused(workspace, monkeypatch) -> None:
    key = workspace / "instance-key"
    key.write_bytes(b"\x00" * 32)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", "instance-key")
    assert exec_reads_secret("cp -b notes.txt instance-key && cat instance-key~")
    assert exec_reads_secret("cp --back notes.txt instance-key")
    assert exec_reads_secret("cat README.md") is None
