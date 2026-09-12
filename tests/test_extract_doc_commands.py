"""The install gate runs the quickstart's own shell, so the extractor must be exact.

Every assertion here exists because a gate that silently extracts the wrong
thing is worse than no gate: it reports green against commands nobody
documented. So the extractor is verbatim or it fails loudly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "extract_doc_commands.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from extract_doc_commands import BlockError, extract_block  # noqa: E402

ONE_BLOCK = """\
# Doc

Prose before.

<!-- install-gate: local -->
```bash
pip install genusos
export OPENROUTER_API_KEY=sk-your-key
genus init --yes --owner-email ada@example.com
```
<!-- /install-gate -->

Prose after.
"""


def test_prints_the_shell_verbatim(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK, encoding="utf-8")

    assert extract_block(doc, "local") == (
        "pip install genusos\n"
        "export OPENROUTER_API_KEY=sk-your-key\n"
        "genus init --yes --owner-email ada@example.com\n"
    )


def test_placeholders_are_not_substituted(tmp_path: Path) -> None:
    """The workflow exports the real values; the extractor never rewrites them.

    An extractor that helpfully substituted would mean CI tested a command the
    docs do not contain.
    """
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK, encoding="utf-8")

    assert "sk-your-key" in extract_block(doc, "local")
    assert "ada@example.com" in extract_block(doc, "local")


def test_missing_block_fails(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK, encoding="utf-8")

    with pytest.raises(BlockError, match="no install-gate block named 'compose'"):
        extract_block(doc, "compose")


def test_two_fenced_blocks_in_one_marker_fails(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(
        "<!-- install-gate: local -->\n"
        "```bash\n"
        "one\n"
        "```\n"
        "```bash\n"
        "two\n"
        "```\n"
        "<!-- /install-gate -->\n",
        encoding="utf-8",
    )

    with pytest.raises(BlockError, match="2 fenced blocks"):
        extract_block(doc, "local")


def test_no_fenced_block_fails(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(
        "<!-- install-gate: local -->\njust prose\n<!-- /install-gate -->\n",
        encoding="utf-8",
    )

    with pytest.raises(BlockError, match="0 fenced blocks"):
        extract_block(doc, "local")


def test_unterminated_marker_fails(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text("<!-- install-gate: local -->\n```bash\none\n```\n", encoding="utf-8")

    with pytest.raises(BlockError, match="never closed"):
        extract_block(doc, "local")


def test_duplicate_named_blocks_fail(tmp_path: Path) -> None:
    """Two blocks with one name means the gate would pick one arbitrarily."""
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK + "\n" + ONE_BLOCK, encoding="utf-8")

    with pytest.raises(BlockError, match="2 install-gate blocks named 'local'"):
        extract_block(doc, "local")


def test_cli_prints_the_block(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(doc), "--block", "local"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == extract_block(doc, "local")


def test_cli_exits_nonzero_on_a_missing_block(tmp_path: Path) -> None:
    doc = tmp_path / "quickstart.md"
    doc.write_text(ONE_BLOCK, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(doc), "--block", "nope"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "no install-gate block named 'nope'" in result.stderr


def test_the_shipped_quickstart_has_both_gate_blocks() -> None:
    """The gate's own contract with the doc it replays."""
    quickstart = REPO_ROOT / "docs" / "quickstart.md"

    for name in ("local", "compose"):
        block = extract_block(quickstart, name)
        assert "genus init" in block
        assert "genus doctor --json" in block
