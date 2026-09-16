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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED = {
    "brain/README.md",
    "local/owner.env.example",
}


def test_no_tracked_instance_files():
    # hatchling's default sdist inclusion follows the VCS file list, so this
    # test ships in the sdist tarball. Extracted from that tarball (or any
    # other non-git checkout — e.g. a wheel's unlikely-but-possible test
    # collection) there is no .git to ask, so skip rather than let `git`
    # fail the collection with a raw CalledProcessError. `.git` is a
    # directory in a normal clone and a file in a worktree — either way its
    # presence means `git ls-files` is meaningful here.
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to ls-files against")

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


#: The secret files Genus itself writes into ``ROBOTHOR_WORKSPACE``. A
#: workspace is very often the checkout — `genus init` in a clone, a container
#: whose working copy IS the instance — so anything written there has to be
#: ignored, or the next `git add -A` commits it. `.vault-key` was already
#: ignored; `.fingerprint-salt` was not, and a salt in the repository is a key
#: every reader holds, which is the whole reason it stopped being a constant.
WORKSPACE_SECRET_FILES = (".vault-key", ".fingerprint-salt")


@pytest.mark.parametrize("name", WORKSPACE_SECRET_FILES)
def test_workspace_secret_files_are_gitignored(name):
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to check-ignore against")

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", name],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 0, (
        f"{name} is written into the workspace and is not gitignored; a checkout "
        "used as a workspace would commit it"
    )


@pytest.mark.parametrize("name", WORKSPACE_SECRET_FILES)
def test_workspace_secret_files_are_also_refused_to_agents(name):
    """The two lists must not drift apart: a file worth hiding from git is a
    file worth refusing to an agent that was told where the workspace is."""
    from robothor.engine.secret_paths import is_secret_path

    assert is_secret_path(f"/workspace/{name}")
