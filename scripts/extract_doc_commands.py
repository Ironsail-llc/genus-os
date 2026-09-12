#!/usr/bin/env python3
"""Print the shell inside a named ``install-gate`` block of a markdown file.

The fresh-install acceptance gate runs the quickstart's own commands, not a
copy of them. A copy drifts: every install defect this cycle -- the dead
`pip install robothor` line, an `init` that wrote no `owner.yaml`, a wizard
whose completion gate closed every route -- was invisible precisely because
nothing executed the documented text. So the workflow pipes the output of this
script into `bash`, and the documentation is the test.

    <!-- install-gate: local -->
    ```bash
    pip install genusos
    ```
    <!-- /install-gate -->

    $ python scripts/extract_doc_commands.py --file docs/quickstart.md --block local
    pip install genusos

Two rules make that safe:

* **Verbatim.** Placeholders the docs use for reader-supplied values
  (`sk-your-key`, `ada@example.com`) are emitted unchanged; the workflow
  exports the real (mock) values into the environment around them. An
  extractor that substituted would mean CI green on a command the docs do not
  contain.
* **Exactly one fenced block per marker.** Zero, two, a duplicate marker name
  or a marker that is never closed are all errors, never a silent best guess.

Exit code 0 = the block was found and printed, 1 = it was not.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OPEN_RE = re.compile(r"^\s*<!--\s*install-gate:\s*(?P<name>[A-Za-z0-9_-]+)\s*-->\s*$")
CLOSE_RE = re.compile(r"^\s*<!--\s*/install-gate\s*-->\s*$")
FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


class BlockError(Exception):
    """The named block is missing, duplicated, or not exactly one fence."""


def _regions(lines: list[str], name: str) -> list[list[str]]:
    """Every ``install-gate: name`` region's body, in document order."""
    regions: list[list[str]] = []
    body: list[str] | None = None
    open_line = 0

    for number, line in enumerate(lines, 1):
        opened = OPEN_RE.match(line)
        if opened is not None:
            if body is not None:
                raise BlockError(
                    f"install-gate block opened at line {open_line} was never closed "
                    f"before another opened at line {number}"
                )
            if opened.group("name") == name:
                body, open_line = [], number
            continue
        if CLOSE_RE.match(line):
            if body is not None:
                regions.append(body)
                body = None
            continue
        if body is not None:
            body.append(line)

    if body is not None:
        raise BlockError(f"install-gate block opened at line {open_line} was never closed")
    return regions


def _fenced_blocks(body: list[str]) -> list[str]:
    """The contents of each fenced code block inside a region."""
    blocks: list[str] = []
    current: list[str] | None = None
    closing = ""

    for line in body:
        if current is None:
            match = FENCE_RE.match(line)
            if match is not None:
                current, closing = [], match.group("fence")
            continue
        if line.startswith(closing) and not line[len(closing) :].strip():
            blocks.append("".join(f"{entry}\n" for entry in current))
            current = None
            continue
        current.append(line)

    if current is not None:
        blocks.append("".join(f"{entry}\n" for entry in current))
    return blocks


def extract_block(path: Path, name: str) -> str:
    """The shell inside ``<!-- install-gate: name -->``, verbatim.

    Raises:
        BlockError: the block is absent, appears more than once, or does not
            hold exactly one fenced code block.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    regions = _regions(lines, name)

    if not regions:
        raise BlockError(f"{path}: no install-gate block named {name!r}")
    if len(regions) > 1:
        raise BlockError(f"{path}: {len(regions)} install-gate blocks named {name!r}; expected 1")

    blocks = _fenced_blocks(regions[0])
    if len(blocks) != 1:
        raise BlockError(
            f"{path}: install-gate block {name!r} holds {len(blocks)} fenced blocks; expected 1"
        )
    return blocks[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", required=True, type=Path, help="Markdown file to read")
    parser.add_argument("--block", required=True, help="install-gate block name")
    args = parser.parse_args(argv)

    try:
        sys.stdout.write(extract_block(args.file, args.block))
    except (BlockError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
