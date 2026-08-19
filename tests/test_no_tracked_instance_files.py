"""Tripwire: no instance files may be tracked under brain/ or local/.

brain/ and local/ are instance-land (CLAUDE.md rule 11) — everything in them
belongs to the operator's machine, not the platform. The only exceptions are
the two deliberately-negated platform files below. Anything else appearing in
`git ls-files` here is a data leak into the public repo (this bit us: aider
history, avatar images, a live searxng secret_key, and instance cron scripts
were tracked for months).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED = {
    "brain/README.md",
    "local/owner.env.example",
}


def test_no_tracked_instance_files():
    out = subprocess.run(
        ["git", "ls-files", "brain/", "local/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked = {line for line in out.splitlines() if line}
    leaked = tracked - ALLOWED
    assert not leaked, (
        "Instance files tracked in the platform repo (untrack with "
        f"`git rm --cached` and gitignore them): {sorted(leaked)}"
    )
