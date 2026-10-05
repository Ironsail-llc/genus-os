"""The Claude Code sandbox a job runs under: Bash fenced by bwrap, files by rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.coding.sandbox import (
    JobPaths,
    build_permissions,
    build_sandbox_settings,
    secret_paths,
)

HOME = Path("/users/alice")
WS = Path("/srv/genus")


def _paths(tmp_path: Path, **kw) -> JobPaths:
    common = tmp_path / "repo" / ".git"
    for sub in ("objects", "refs/heads/genus", "logs/refs/heads/genus", "hooks", "worktrees/j1"):
        (common / sub).mkdir(parents=True, exist_ok=True)
    (common / "config").write_text("")
    base = {
        "home": HOME,
        "workspace": WS,
        "worktree": tmp_path / "wt" / "j1",
        "git_common_dir": common,
        "git_admin_dir": common / "worktrees" / "j1",
        "branch": "genus/cc-12345678",
    }
    base.update(kw)
    return JobPaths(**base)


def test_secret_paths_cover_credentials_the_claude_login_and_the_instance():
    found = set(secret_paths(HOME, WS))
    for rel in (
        ".ssh",
        ".gnupg",
        ".aws",
        ".netrc",
        ".docker",
        ".kube",
        ".config",
        ".claude",
        ".claude.json",
        ".robothor",
    ):
        assert str(HOME / rel) in found, rel
    assert "/run/robothor" in found
    assert str(WS / "brain") in found
    assert str(WS / ".robothor") in found


def test_code_mode_sandbox_is_on_fails_closed_and_has_no_escape_hatch(tmp_path):
    paths = _paths(tmp_path)
    sb = build_sandbox_settings("code", paths, allowed_domains=())["sandbox"]

    assert sb["enabled"] is True
    assert sb["failIfUnavailable"] is True
    assert sb["allowUnsandboxedCommands"] is False
    assert sb["autoAllowBashIfSandboxed"] is True
    assert sb["network"]["allowedDomains"] == []
    assert str(HOME / ".ssh") in sb["filesystem"]["denyRead"]


def test_code_mode_writes_the_worktree_and_only_the_git_state_a_commit_needs(tmp_path):
    paths = _paths(tmp_path)
    fs = build_sandbox_settings("code", paths, allowed_domains=())["sandbox"]["filesystem"]
    common = paths.git_common_dir

    assert str(paths.worktree) in fs["allowWrite"]
    assert str(common / "objects") in fs["allowWrite"]
    assert str(common / "refs" / "heads" / "genus") in fs["allowWrite"]
    assert str(common / "logs" / "refs" / "heads" / "genus") in fs["allowWrite"]
    assert str(paths.git_admin_dir) in fs["allowWrite"]
    # Never the whole git dir: hooks and config are code execution.
    assert str(common) not in fs["allowWrite"]
    assert str(common / "hooks") in fs["denyWrite"]
    assert str(common / "config") in fs["denyWrite"]


def test_code_mode_network_comes_from_the_setting(tmp_path):
    sb = build_sandbox_settings("code", _paths(tmp_path), allowed_domains=("pypi.org",))
    assert sb["sandbox"]["network"]["allowedDomains"] == ["pypi.org"]


@pytest.mark.parametrize("mode", ["review", "readonly"])
def test_read_only_modes_write_nothing_and_never_reach_the_network(tmp_path, mode):
    paths = _paths(tmp_path, branch=None)
    sb = build_sandbox_settings(mode, paths, allowed_domains=("pypi.org",))["sandbox"]

    assert sb["enabled"] is True
    assert sb["network"]["allowedDomains"] == []
    assert sb["filesystem"]["allowWrite"] == []
    # autoAllowBashIfSandboxed allows EVERY Bash command once sandboxed,
    # bypassing the read-only allowlist (probed: `git grep -O` ran in review
    # mode with it on). Read-only modes keep the allowlist in charge.
    assert sb["autoAllowBashIfSandboxed"] is False
    # The sandbox lets the working directory be written by default; deny it.
    assert str(paths.worktree) in sb["filesystem"]["denyWrite"]


def _abs(p: Path) -> str:
    return "/" + str(p)


def test_code_mode_edits_are_confined_to_the_worktree(tmp_path):
    paths = _paths(tmp_path)
    allowed, denied = build_permissions("code", paths)

    assert "Edit" not in allowed and "Write" not in allowed
    assert f"Edit({_abs(paths.worktree)}/**)" in allowed
    assert f"Write({_abs(paths.worktree)}/**)" in allowed
    assert "Bash" in allowed


@pytest.mark.parametrize("mode", ["code", "review", "readonly"])
def test_every_mode_denies_file_tools_on_the_secret_paths(tmp_path, mode):
    paths = _paths(tmp_path)
    _, denied = build_permissions(mode, paths)

    for tool in ("Read", "Edit", "Write"):
        assert f"{tool}({_abs(HOME / '.ssh')}/**)" in denied, tool
        assert f"{tool}({_abs(HOME / '.claude.json')})" in denied, tool
    assert f"Read({_abs(WS / 'brain')}/**)" in denied
    assert "WebFetch" in denied and "Bash(git push:*)" in denied


@pytest.mark.parametrize("mode", ["review", "readonly"])
def test_read_only_bash_has_no_git_grep_and_no_writing_or_external_forms(tmp_path, mode):
    allowed, denied = build_permissions(mode, _paths(tmp_path))

    assert not any("git grep" in rule for rule in allowed)
    assert "Bash" not in allowed
    for flag in ("--output", "--ext-diff", "--no-index"):
        assert any(flag in rule for rule in denied), flag
    assert "Edit" in denied and "Write" in denied
