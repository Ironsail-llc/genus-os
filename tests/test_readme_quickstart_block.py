"""README's Quick Start must be the block CI actually replays.

The README says "CI replays these exact four lines on a fresh machine every
night". That is true only while the snippet equals the `local` region of
``docs/quickstart.md`` -- the one
``.github/workflows/install-gate.yml`` extracts and runs. Nothing kept the two
in step: the workflow's ``pull_request.paths`` does not list ``README.md``, so a
README edit ships with no gate at all, and the gate's own green tick says
nothing about a copy it never read.

This test is that missing link. If the README drifts, fix the README (or the
quickstart), never this assertion -- the sentence is a claim about CI, and the
only honest way to keep it is to keep the bytes identical.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = REPO_ROOT / "scripts" / "extract_doc_commands.py"
QUICKSTART = REPO_ROOT / "docs" / "quickstart.md"
README = REPO_ROOT / "README.md"

#: The sentence that makes the claim. If it is reworded away, this test's
#: premise is gone and the test should go with it.
CLAIM = "CI replays these exact four lines"


def _extractor():
    spec = importlib.util.spec_from_file_location("extract_doc_commands", EXTRACTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _readme_quickstart_block() -> str:
    """The first fenced bash block after the Quick Start heading."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "## Quick Start")
    opened: int | None = None
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if opened is None:
            if stripped.startswith("```"):
                opened = index
            continue
        if stripped == "```":
            return "\n".join(lines[opened + 1 : index]) + "\n"
    raise AssertionError("README has no fenced block under '## Quick Start'")


def test_readme_quickstart_is_the_replayed_block() -> None:
    gate_block = _extractor().extract_block(QUICKSTART, "local")
    assert _readme_quickstart_block() == gate_block, (
        "README's Quick Start snippet no longer equals the `local` install-gate "
        "block in docs/quickstart.md, so the README's claim that CI replays it "
        "is false. Copy the block across, or reword the claim."
    )


def test_the_claim_is_still_the_one_this_test_backs() -> None:
    assert CLAIM in README.read_text(encoding="utf-8"), (
        f"README no longer says {CLAIM!r}; if the claim was dropped on purpose, "
        "drop this test with it rather than leaving it asserting nothing"
    )
