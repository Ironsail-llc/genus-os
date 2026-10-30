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


@pytest.mark.parametrize(
    "command",
    [
        # An interpreter's FILE operands are still files: only its program
        # text (the value of -c / -e / -pe …) is exempt from the glob rule.
        "perl -ne print .en?",
        "python3 script.py .en?",
        "ruby -e 'puts 1' .en*",
        # The shell expands a glob character only where it is UNQUOTED: a
        # partly quoted word still globs, and a quoted copy of a word does
        # not exempt an unquoted spelling of it elsewhere.
        "cat .en[v' ']",
        "cat '.en?' '.e'n?",
        'head .e"n"?',
    ],
)
def test_only_unquoted_globs_in_file_operands_are_exempt(workspace, command) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A shell quote may span a newline: the glob after it is NOT quoted.
        "echo 'a\n' ; cat .en?",
        'echo "a\n" ; cat .en*',
        # An unclosed quote cannot be judged at all: fail closed.
        "echo 'a ; cat .en?",
    ],
)
def test_quote_state_follows_the_whole_command(workspace, command) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize(
    "command",
    [
        # A cluster that ENDS in e/c may be an option with an attached value
        # (-rtime), not the program option: the file after it is still a file.
        "ruby -ne print -rtime .en?",
        "perl -ne print -Mstrict .en?",
    ],
)
def test_an_attached_option_value_is_not_the_program(workspace, command) -> None:
    assert exec_reads_secret(command), command


def test_multi_line_commands_with_closed_quotes_still_run(workspace) -> None:
    assert exec_reads_secret("echo 'a\n' ; cat notes.txt") is None
    assert exec_reads_secret("perl -ne 'print if /[0-9]/' log") is None
    assert exec_reads_secret("ruby -ne 'puts $_' -rtime log") is None


@pytest.mark.parametrize(
    "command",
    [
        # After a quote that spans lines the later arguments must still be
        # tokenised with their quotes removed, so `.e"n"v` is `.env`.
        "echo 'a\n' ; cat .e\"n\"v",
        "echo 'a\n' ; head .e'n'v",
    ],
)
def test_arguments_after_a_multi_line_quote_are_still_judged(workspace, command) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize(
    "command",
    [
        # An UNQUOTED newline ends a command like `;` does: the second line
        # is its own command, whatever the first line was.
        "ls\ncat .env",
        "true\nprintenv",
        "echo hi\nhead .env",
        "cd sub\ncat .env",
        # The shell runs the complete lines before it reports an unclosed
        # quote, so the fallback must keep the line boundary too.
        "ls\ncat .env\necho 'x",
        # A trailing comment ends at the newline; it must not swallow it.
        "cd repo  # go there\ncat .env",
        "ls # x\nprintenv",
        "echo ok # don't\nhead .env",
        # A comment is a comment: apostrophes in it are not quotes, so two
        # contractions in two comments cannot pair up and hide the line
        # between them.
        "# Let's look at the config\ncat .env\n# That's all",
        "ls  # don't care\ncat .env\necho ok  # that's it",
        "cat .env  # it's here",
        # Once a later line leaves a quote unclosed, the complete lines
        # before it are still lexed properly: a `#` inside quotes is not a
        # comment and an operator stuck to a word still separates commands.
        'echo "a # b" ; cat .env\necho \'',
        "echo hi; cat .env\necho '",
        # After the first line that cannot be read, the quote state is
        # unknown: a continuation line may close the quote and run a
        # command, so every later line is judged loosely too.
        "echo '\n\"' ; cat .env #\"\necho '",
        # The loose split still separates a command glued to an operator.
        "echo '\n'\necho hi;cat .env\necho '",
        "echo hi;cat .env; echo '",
    ],
)
def test_an_unquoted_newline_separates_commands(workspace, command) -> None:
    assert exec_reads_secret(command), command


def test_a_newline_separated_command_without_a_secret_runs(workspace) -> None:
    assert exec_reads_secret("ls\ncat notes.txt") is None
    assert exec_reads_secret("true\n\nhead config.yaml") is None
    assert exec_reads_secret("# Let's look\ncat notes.txt\n# That's all") is None
    assert exec_reads_secret("cat notes.txt  # don't mind the .env mention") is None


@pytest.mark.parametrize(
    "command",
    [
        # -f / --file / --from-file name a program FILE, not program text:
        # the value stays under the glob and moved-key rules.
        "awk -f .en? /dev/null",
        "sed -f .env notes.txt",
        "jq --from-file=.en? data.json",
        "grep -f .en? notes.txt",
    ],
)
def test_a_program_file_option_value_is_a_file(workspace, command) -> None:
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
