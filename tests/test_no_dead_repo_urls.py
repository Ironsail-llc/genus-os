"""Guard against dead repository clone/reference URLs creeping back into docs.

The repo has moved (and been renamed) more than once. Two URL forms are
dead and must never appear in tracked Markdown:

  - github.com/genusos/genusos
  - github.com/robothor-ai/robothor

The live repository is github.com/Ironsail-llc/genus-os.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEAD_PATTERNS = (
    "github.com/genusos/genusos",
    "github.com/robothor-ai/robothor",
)


def _tracked_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def test_no_dead_repo_urls_in_tracked_markdown():
    hits: list[str] = []
    for path in _tracked_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(pattern in line for pattern in DEAD_PATTERNS):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not hits, "dead repository URLs found in docs:\n" + "\n".join(hits)
