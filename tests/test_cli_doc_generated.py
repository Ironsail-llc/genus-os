"""``docs/reference/cli.md`` is generated, not written.

The committed file must equal what ``scripts/gen_cli_doc.py`` renders from the
real argparse parser, so a verb renamed or a flag dropped without regenerating
fails here rather than shipping a reference that disagrees with the code.

Regenerate with::

    python scripts/gen_cli_doc.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen_cli_doc.py"
REFERENCE = REPO_ROOT / "docs" / "reference" / "cli.md"

#: The verbs the one install path is documented in terms of. A reference that
#: has lost one of these is not a reference an operator can follow.
REQUIRED_HEADINGS = (
    "## `genus init`",
    "## `genus doctor`",
    "## `genus config`",
    "### `genus config get`",
    "### `genus config set`",
    "### `genus config explain`",
    "### `genus config validate`",
    "## `genus migrate`",
    "### `genus auth setup-link`",
)


def _generator():
    spec = importlib.util.spec_from_file_location("gen_cli_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reference_is_committed() -> None:
    assert REFERENCE.exists(), f"{REFERENCE} is missing -- run {GENERATOR.name}"


def test_reference_equals_generator_output() -> None:
    rendered = _generator().render()
    assert REFERENCE.read_text(encoding="utf-8") == rendered, (
        "docs/reference/cli.md is stale -- run `python scripts/gen_cli_doc.py`"
    )


def test_reference_documents_the_install_path_verbs() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    for heading in REQUIRED_HEADINGS:
        assert heading in text, f"{heading} is missing from the CLI reference"
    assert "`genus migrate --status`" in text or "--status" in text, (
        "the migration ledger flag must be documented"
    )


def test_reference_quotes_no_shell_fence() -> None:
    """Usage lines are inline code on purpose.

    ``scripts/check_doc_commands.py`` parses every command in a shell fence.
    A usage line is not a runnable command -- putting one in a fence would
    either fail that gate or force it to be taught to ignore real breakage.
    """
    text = REFERENCE.read_text(encoding="utf-8")
    assert "```" not in text, "the CLI reference must carry no fenced code block"
