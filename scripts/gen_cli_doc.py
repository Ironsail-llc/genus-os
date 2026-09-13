#!/usr/bin/env python3
"""Render ``docs/reference/cli.md`` from the real argparse parser.

Every verb and flag in the reference is walked out of ``_build_parser()``, the
same parser ``genus`` dispatches on, so a renamed subcommand or a dropped flag
changes the committed file in the pull request that renames it.
``tests/test_cli_doc_generated.py`` asserts the two agree.

The page deliberately contains **no fenced code block**: every command it
quotes is inline code. ``scripts/check_doc_commands.py`` parses shell fences,
and a usage line full of ``[--flag]`` brackets is not a command anybody can
run — it would either fail that gate or teach it to ignore real breakage.

Usage::

    python scripts/gen_cli_doc.py          # write the file
    python scripts/gen_cli_doc.py --check  # exit 1 if it is stale
    python scripts/gen_cli_doc.py --stdout # print, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "reference" / "cli.md"

HEADER = """<!--
GENERATED FILE — do not edit.

Rendered from robothor/cli/_build_parser() by scripts/gen_cli_doc.py.
Add or rename a verb there and re-run the script; the committed file and the
generator output are compared in tests/test_cli_doc_generated.py.
-->

# CLI reference

Every verb the `genus` command accepts, with its flags and their defaults.

`genus` is the documented name. `genusos` and `robothor` are aliases for the
same entry point — existing systemd units, cron entries and older runbooks
invoke them, and they keep working.

Where to start, rather than reading this top to bottom:

| I want to… | Command |
| --- | --- |
| install an instance | `genus init` — and see the [quick start](../quickstart.md) |
| find out why an instance is unhealthy | `genus doctor` |
| repair what a diagnostic can repair | `genus doctor --fix` |
| read or change a setting | `genus config get`, `genus config set`, `genus config explain` |
| apply the schema, or see where it stands | `genus migrate`, `genus migrate --status` |
| get back into a fresh instance headlessly | `genus auth setup-link` |

Settings are documented in the [configuration reference](configuration.md),
not here: a flag belongs to one command, a setting to the whole instance.
"""

FOOTER = """
## Exit codes

Two commands make promises about their exit code, because scripts gate on them:

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `genus init` | the instance is initialized | a required check failed and **nothing was written** | — |
| `genus doctor` | no `required` check failed | a `required` check failed | the doctor could not run (an unknown `--only` id, a registry that would not import) |

`genus doctor`'s 2 is deliberately separate from its 1: "nothing is wrong" and
"nothing was checked" must never share an exit code, or a typo in a CI gate
becomes a permanently green build.
"""


def _escape(text: str) -> str:
    """Make help text safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _default_cell(action: argparse.Action) -> str:
    default = action.default
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "off" if default is False else "on"
    if default is None or default == "":
        return "—"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    return f"`{default}`"


def _argument_cell(action: argparse.Action) -> str:
    """What the flag takes, if anything."""
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "—"
    if action.choices:
        return " \\| ".join(f"`{choice}`" for choice in action.choices)
    metavar = action.metavar or action.dest.upper()
    return f"`{metavar}`"


def _is_documented(action: argparse.Action) -> bool:
    if isinstance(action, argparse._HelpAction):
        return False
    if isinstance(action, argparse._SubParsersAction):
        return False
    return action.help != argparse.SUPPRESS


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _positionals(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if _is_documented(a) and not a.option_strings]


def _options(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if _is_documented(a) and a.option_strings]


def _usage(path: str, parser: argparse.ArgumentParser) -> str:
    """A readable usage line, built from the parser's own actions."""
    parts = [path]
    sub = _subparsers(parser)
    if sub is not None:
        parts.append("{" + ",".join(sub.choices) + "}")
    for action in _positionals(parser):
        name = action.metavar or action.dest
        parts.append(f"<{name}>" if action.nargs not in ("?", "*") else f"[{name}]")
    for action in _options(parser):
        flag = action.option_strings[0]
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            parts.append(f"[{flag}]")
        else:
            metavar = action.metavar or action.dest.upper()
            parts.append(f"[{flag} {metavar}]")
    return " ".join(parts)


def _render_arguments(lines: list[str], parser: argparse.ArgumentParser) -> None:
    positionals = _positionals(parser)
    options = _options(parser)
    if positionals:
        lines.append("\n| Argument | Required | Description |\n")
        lines.append("| --- | --- | --- |\n")
        for action in positionals:
            name = action.metavar or action.dest
            required = "no" if action.nargs in ("?", "*") else "yes"
            lines.append(f"| `{name}` | {required} | {_escape(action.help or '')} |\n")
    if options:
        lines.append("\n| Flag | Takes | Default | Description |\n")
        lines.append("| --- | --- | --- | --- |\n")
        for action in options:
            flags = ", ".join(f"`{flag}`" for flag in action.option_strings)
            lines.append(
                "| {flags} | {takes} | {default} | {help} |\n".format(
                    flags=flags,
                    takes=_argument_cell(action),
                    default=_default_cell(action),
                    help=_escape(action.help or ""),
                )
            )


def _verb_help(sub: argparse._SubParsersAction, name: str) -> str:
    for choice in sub._choices_actions:
        if choice.dest == name:
            return _escape(choice.help or "")
    return ""


def render() -> str:
    """The full contents of docs/reference/cli.md."""
    sys.path.insert(0, str(REPO_ROOT))
    from robothor.cli import _build_parser

    parser = _build_parser()
    top = _subparsers(parser)
    assert top is not None, "the CLI parser has no subcommands"

    lines = [HEADER]
    lines.append(f"\n{len(top.choices)} verbs.\n")

    for name, verb_parser in top.choices.items():
        lines.append(f"\n## `genus {name}`\n")
        help_text = _verb_help(top, name)
        if help_text:
            lines.append(f"\n{help_text}.\n")
        nested = _subparsers(verb_parser)
        lines.append(f"\nUsage: `{_usage(f'genus {name}', verb_parser)}`\n")
        _render_arguments(lines, verb_parser)
        if nested is not None:
            for sub_name, sub_parser in nested.choices.items():
                lines.append(f"\n### `genus {name} {sub_name}`\n")
                sub_help = _verb_help(nested, sub_name)
                if sub_help:
                    lines.append(f"\n{sub_help}.\n")
                lines.append(f"\nUsage: `{_usage(f'genus {name} {sub_name}', sub_parser)}`\n")
                _render_arguments(lines, sub_parser)

    lines.append(FOOTER)
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    cli.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = cli.parse_args(argv)

    rendered = render()
    if args.stdout:
        sys.stdout.write(rendered)
        return 0
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT} is stale — run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
