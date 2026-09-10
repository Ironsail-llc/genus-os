#!/usr/bin/env python3
"""CI gate: every CLI command quoted in the docs must actually parse.

The published Getting Started opened with `pip install robothor`. That package
does not exist -- the distribution is `genusos` (`pyproject.toml`) -- so the
first line a new user copied failed. Nothing caught it, because nothing had
ever parsed a command printed in the documentation.

What this does: walks the shipped markdown, pulls the shell commands out of
fenced code blocks, and feeds each `robothor`/`genusos`/`genus` invocation to
the real `robothor.cli` parser. A renamed or deleted subcommand now fails in
the same pull request that renames it. `pip install robothor` fails outright.

Escape hatch: put `<!-- doc-check: skip -->` on the line before a fence to
skip that one block -- for illustrative or intentionally-broken snippets. It
is not for hiding a command that no longer exists; fix the doc instead.

Exit code 0 = every quoted command parses, 1 = at least one does not.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

# Console scripts declared in pyproject.toml, plus the short brand name the
# docs occasionally use. All three route to the same parser.
CLI_PREFIXES = ("robothor", "genusos", "genus")

# Fence info strings whose contents are shell, and therefore checkable.
SHELL_LANGUAGES = frozenset({"bash", "sh", "shell", "console"})

SKIP_MARKER = "<!-- doc-check: skip -->"

FENCE_RE = re.compile(r"^(?P<indent>\s*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# `robothor` was never the distribution name; `genusos` is.
PIP_INSTALL_ROBOTHOR_RE = re.compile(r"pip install robothor\b")

# Placeholder shapes the docs use for values the reader supplies.
PLACEHOLDER_RE = re.compile(r"<[^<>]+>|\$\{[^{}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

# Shell control operators. Everything after the first one is a different
# command (or a pipe target) and is not ours to parse.
SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>"})

# Files this gate covers: the whole shipped docs tree plus the three top-level
# documents a reader meets first.
TOP_LEVEL_DOCS = ("README.md", "SERVICES.md", "CONTRIBUTING.md")

# Dated session artifacts: a plan or spec written on one day, never edited
# again, and not part of the shipped documentation. Rewriting a historical
# record to satisfy a gate makes the record less true, not more.
# Broader than `scripts/check_instance_leak.py`, which exempts only
# `docs/superpowers/plans/`: the sibling `specs/` are design documents that
# name components as they were proposed, including files never built, so a
# path gate has even less business editing them than a leak gate does.
ARCHIVED_DOC_DIRS = ("docs/superpowers/",)


@dataclass(frozen=True)
class Finding:
    """One documented command that will not work."""

    path: str
    line: int
    command: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.command} -- {self.reason}"


@dataclass(frozen=True)
class _Block:
    """A fenced code block: its language, body lines, and where they start."""

    language: str
    lines: list[tuple[int, str]]


def documents(repo_root: Path) -> list[Path]:
    """Every markdown file this gate checks, in a stable order."""
    paths = [
        path
        for path in sorted((repo_root / "docs").rglob("*.md"))
        if not str(path.relative_to(repo_root)).startswith(ARCHIVED_DOC_DIRS)
    ]
    paths.extend(repo_root / name for name in TOP_LEVEL_DOCS)
    # The runnable examples are the second thing a new user copies from, and
    # all three of them opened with `pip install robothor` too.
    paths.extend(sorted((repo_root / "examples").glob("*/README.md")))
    return [path for path in paths if path.is_file()]


def _iter_blocks(text: str) -> list[_Block]:
    """Fenced code blocks, minus any preceded by the skip marker."""
    blocks: list[_Block] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opening = FENCE_RE.match(lines[index])
        if opening is None:
            index += 1
            continue

        fence = opening.group("fence")
        info = opening.group("info").strip()
        language = info.lstrip("{").split()[0].lower() if info else ""
        skipped = _preceded_by_skip_marker(lines, index)

        body: list[tuple[int, str]] = []
        index += 1
        while index < len(lines):
            closing = FENCE_RE.match(lines[index])
            if (
                closing is not None
                and closing.group("fence")[0] == fence[0]
                and len(closing.group("fence")) >= len(fence)
                and not closing.group("info").strip()
            ):
                break
            body.append((index + 1, lines[index]))
            index += 1
        index += 1

        if not skipped:
            blocks.append(_Block(language=language, lines=body))
    return blocks


def _preceded_by_skip_marker(lines: list[str], fence_index: int) -> bool:
    """True when the nearest non-blank line above the fence is the marker."""
    cursor = fence_index - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    return cursor >= 0 and lines[cursor].strip() == SKIP_MARKER


def _strip_prompt(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("$ "):
        return stripped[2:].strip()
    return stripped


def _is_cli_invocation(line: str) -> bool:
    return any(line.startswith(f"{prefix} ") or line == prefix for prefix in CLI_PREFIXES)


def _is_checkable(block: _Block) -> bool:
    """Tagged shell blocks always; untagged blocks only if they open with a CLI call."""
    if block.language in SHELL_LANGUAGES:
        return True
    if block.language:
        return False
    for _, raw in block.lines:
        if not raw.strip():
            continue
        return _is_cli_invocation(_strip_prompt(raw))
    return False


def _substitute_placeholders(token: str) -> str:
    """Replace `<value>` / `${VAR}` / `$VAR` spans so the parser sees a value."""
    return PLACEHOLDER_RE.sub("x", token)


def _argv(command: str) -> list[str] | None:
    """Tokenise a documented command, or None if it is not parseable shell."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None
    argv: list[str] = []
    for token in tokens:
        if token in SHELL_OPERATORS:
            break
        argv.append(_substitute_placeholders(token))
    return argv or None


@functools.lru_cache(maxsize=1)
def _build_cli_parser() -> argparse.ArgumentParser:
    """The real CLI parser, made to raise instead of exiting where it can.

    Imported here rather than at module scope so an import failure surfaces
    as a crash of this gate, not as a silently skipped check: a checker that
    cannot reach the parser must go red, never green.
    """
    from robothor.cli import _build_parser

    parser = _build_parser()
    _disable_exit(parser)
    return parser


def _disable_exit(parser: argparse.ArgumentParser) -> None:
    """Set `exit_on_error=False` on a parser and every subparser under it."""
    if hasattr(parser, "exit_on_error"):
        parser.exit_on_error = False
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        for sub in choices.values():
            if isinstance(sub, argparse.ArgumentParser):
                _disable_exit(sub)


def _parses(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    """True when the CLI parser accepts this command line.

    Two things this has to get right, and the first version got both wrong.

    `parse_known_args` does not reject an unknown flag -- it hands it back in
    the second element of the tuple. Discarding that return made the checker
    blind to the exact defect it exists to catch: `robothor migrate --status`,
    a flag that has never existed, parsed clean. Unrecognised *positionals*
    stay tolerated (the docs write `[--tenant TENANT]`-style notation and
    bracket-optional arguments), but a leftover starting with `-` is a flag
    the CLI does not have.

    And argparse raises SystemExit for `--help` with code 0, for a parse
    error with code 2. Treating every SystemExit as failure reported a
    documented `--help` as broken, which pushes authors toward the skip
    marker instead of toward a fix.
    """
    noise = io.StringIO()
    try:
        with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(noise):
            _, extra = parser.parse_known_args(argv[1:])
    except SystemExit as exc:
        return exc.code in (0, None)
    except (argparse.ArgumentError, ValueError):
        return False
    return not any(token.startswith("-") for token in extra)


def check_markdown(
    text: str, path: str, parser: argparse.ArgumentParser | None = None
) -> list[Finding]:
    """Every documented command in `text` that will not work."""
    if parser is None:
        parser = _build_cli_parser()

    findings: list[Finding] = []
    for block in _iter_blocks(text):
        if not _is_checkable(block):
            continue
        for lineno, raw in block.lines:
            command = _strip_prompt(raw)
            if not command:
                continue

            if PIP_INSTALL_ROBOTHOR_RE.search(command):
                findings.append(
                    Finding(
                        path=path,
                        line=lineno,
                        command=command,
                        reason="no such package -- the distribution is `genusos`",
                    )
                )
                continue

            if not _is_cli_invocation(command):
                continue

            argv = _argv(command)
            if argv is None:
                continue
            if not _parses(parser, argv):
                findings.append(
                    Finding(
                        path=path,
                        line=lineno,
                        command=command,
                        reason="does not parse against the robothor CLI",
                    )
                )
    return findings


def check_file(path: Path, repo_root: Path) -> list[Finding]:
    """Check one markdown file, reporting paths relative to the repo root."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return check_markdown(text, str(path.relative_to(repo_root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root to scan (default: the repo this script lives in)",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    cli_parser = _build_cli_parser()
    files = documents(repo_root)
    findings: list[Finding] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(check_markdown(text, str(path.relative_to(repo_root)), cli_parser))

    if findings:
        print("DOCUMENTED COMMANDS THAT DO NOT WORK:")
        for finding in findings:
            print(f"  {finding}")
        print(
            f"\n{len(findings)} issue(s). Fix the documentation -- "
            f"'{SKIP_MARKER}' before a fence is for illustrative snippets only."
        )
        return 1

    print(f"check_doc_commands: {len(files)} file(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
