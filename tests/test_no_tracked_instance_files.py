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


# ── Skills: the platform ships its own, the instance keeps the rest ──
#
# `agents/skills/` is platform code. A skill an agent WROTE landing there is
# the same leak as a tracked `brain/` file, with an extra edge: a clean
# checkout of the platform deletes it, so the instance loses what it learned.
# Runtime writes go to `instance_skills_dir()` (gitignored); this is the
# tripwire for anything that gets there anyway.


def _tracked_skill_metas() -> list[str]:
    return [
        line
        for line in subprocess.run(
            ["git", "ls-files", "agents/skills/"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if line.endswith("/meta.json")
    ]


def test_no_tracked_skill_carries_an_instance_origin():
    import json

    from robothor.engine.skills import INSTANCE_ORIGIN, skill_origin

    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to ls-files against")

    leaked = []
    for rel in _tracked_skill_metas():
        try:
            meta = json.loads((REPO_ROOT / rel).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and skill_origin(meta) == INSTANCE_ORIGIN:
            leaked.append(rel)

    assert not leaked, (
        "these skills are marked as instance-created but are tracked in the "
        "platform tree — move them with `genus skills migrate-instance`, or "
        f'stamp `"origin": "platform"` if the platform is adopting them: {sorted(leaked)}'
    )


def test_every_tracked_skill_declares_the_platform_owns_it():
    """A bundled skill says so, so the migration can never carry it off.

    Several bundled skills were agent-written before they were adopted and
    still carry `auto_generated`. Without an explicit marker the migration
    would read that as instance data and move a tracked directory out of the
    checkout.
    """
    import json

    from robothor.engine.skills import PLATFORM_ORIGIN

    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to ls-files against")

    unmarked = []
    for rel in _tracked_skill_metas():
        try:
            meta = json.loads((REPO_ROOT / rel).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("origin") != PLATFORM_ORIGIN:
            unmarked.append(rel)

    assert not unmarked, (
        'every tracked skill meta.json needs `"origin": "platform"` — the '
        f"marker is what keeps the instance migration off it: {sorted(unmarked)}"
    )


def test_the_instance_skills_directory_is_gitignored():
    """The default instance skills dir is inside the checkout on most installs."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to check-ignore against")

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "brain/skills/example/SKILL.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 0, (
        "brain/skills/ is where agents write their skills and is not gitignored; "
        "a checkout used as a workspace would commit them"
    )
