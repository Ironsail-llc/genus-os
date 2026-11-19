"""The Claude Code sandbox a job runs under: Bash fenced by bwrap, files by rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.coding.runner import MODES
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


@pytest.mark.parametrize("mode", ["review", "readonly"])
def test_read_only_bash_can_find_the_merge_base_but_not_write_a_merge(tmp_path, mode):
    """The review prompt asks for ``git merge-base``; ``git merge-tree --write-tree``
    writes objects into the shared git directory, so it stays out: the service
    computes merge conflicts itself before the job starts."""
    allowed, _ = build_permissions(mode, _paths(tmp_path))

    assert "Bash(git merge-base:*)" in allowed
    assert not any("merge-tree" in rule for rule in allowed)


# ── job homes on the coding workspace ─────────────────────────────────


def _covers(entries: list[str], target: Path) -> bool:
    return any(target == Path(e) or Path(e) in target.parents for e in entries)


@pytest.mark.parametrize("retired_switch", [None, "0"])
def test_no_job_can_read_another_jobs_home_on_the_coding_workspace(
    tmp_path, monkeypatch, retired_switch
):
    """Job homes moved off the denied ~/.config: each job is still denied every other's.

    No publishing switch exists; absent or set to "0", the deny list is the same.
    """
    from robothor.engine.coding.env import job_config_dir
    from robothor.settings import reset_settings

    if retired_switch is None:
        monkeypatch.delenv("ROBOTHOR_CODING_PR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ROBOTHOR_CODING_PR_ENABLED", retired_switch)
    monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", str(tmp_path / "coding"))
    reset_settings()
    home_a, home_b = job_config_dir("job-a"), job_config_dir("job-b")

    for own, other in ((home_a, home_b), (home_b, home_a)):
        paths = _paths(tmp_path / own.name)
        for mode in MODES:
            deny_read = build_sandbox_settings(mode, paths, allowed_domains=())["sandbox"][
                "filesystem"
            ]["denyRead"]
            assert _covers(deny_read, other), (mode, deny_read)
            _, denied = build_permissions(mode, paths)
            for tool in ("Read", "Edit", "Write"):
                assert any(
                    r.startswith(f"{tool}(/") and _covers([r[len(tool) + 2 : -4]], other)
                    for r in denied
                    if r.endswith("/**)")
                ), (mode, tool)


def test_without_a_coding_workspace_no_extra_home_path_is_denied(tmp_path, monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CODING_WORKSPACE", raising=False)
    reset_settings()
    assert not [p for p in secret_paths(HOME, WS) if p.endswith("/.home")]


# ── credential files the engine is configured with ───────────────────

#: Settings that name a credential file or directory the engine holds. Set to
#: generic locations outside every statically denied path, so a pass proves the
#: deny came from the setting.
_GWS_FILE = Path("/opt/gws-auth/credentials.json")
_GWS_CONFIG = Path("/var/cache/gws-cli")


@pytest.fixture
def configured(monkeypatch):
    """Set credential-file settings for one test; leave the settings clean after."""
    from robothor.settings import reset_settings

    names = (
        "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
        "GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
        "GENUS_VAULT_KEY_FILE",
        "ROBOTHOR_VAULT_KEY_FILE",
        "GENUS_FINGERPRINT_SALT_FILE",
        "ROBOTHOR_FINGERPRINT_SALT_FILE",
        "ROBOTHOR_ENV_FILE",
        "ROBOTHOR_SECRETS_BACKEND_FILE",
        "ROBOTHOR_SECRETS_FILE",
        "ROBOTHOR_INSTANCE_ENV",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    def _set(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    _set()
    yield _set
    for name in names:
        monkeypatch.delenv(name, raising=False)
    reset_settings()


def _denied_everywhere(tmp_path: Path, mode: str, target: Path) -> None:
    """``target`` is on the Bash sandbox's denyRead and under a Read/Edit/Write deny rule."""
    paths = _paths(tmp_path)
    deny_read = build_sandbox_settings(mode, paths, allowed_domains=())["sandbox"]["filesystem"][
        "denyRead"
    ]
    assert _covers(deny_read, target), (mode, target, deny_read)
    _, denied = build_permissions(mode, paths)
    for tool in ("Read", "Edit", "Write"):
        exact = {r[len(tool) + 2 : -1] for r in denied if r.startswith(f"{tool}(/")}
        dirs = {e[:-3] for e in exact if e.endswith("/**")}
        assert str(target) in exact or _covers(sorted(dirs), target), (mode, tool, target)


@pytest.mark.parametrize("mode", MODES)
def test_the_chart_credential_mounts_are_denied_with_no_setting_at_all(tmp_path, configured, mode):
    """The chart mounts every credential an engine pod holds under /etc/genus
    (the Workspace OAuth file, the vault master key and salt). A job is denied
    the whole tree whatever the environment says."""
    for target in (
        Path("/etc/genus"),
        Path("/etc/genus/gws/credentials.json"),
        Path("/etc/genus/vault/.vault-key"),
    ):
        _denied_everywhere(tmp_path, mode, target)


@pytest.mark.parametrize("mode", MODES)
def test_the_workspace_cli_credential_file_its_directory_and_token_cache_are_denied(
    tmp_path, configured, mode
):
    """A mounted Google Workspace credential was readable by a coding job."""
    configured(
        GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=str(_GWS_FILE),
        GOOGLE_WORKSPACE_CLI_CONFIG_DIR=str(_GWS_CONFIG),
    )
    _denied_everywhere(tmp_path, mode, _GWS_FILE)
    _denied_everywhere(tmp_path, mode, _GWS_FILE.parent)
    _denied_everywhere(tmp_path, mode, _GWS_FILE.parent / "token-cache.json")
    _denied_everywhere(tmp_path, mode, _GWS_CONFIG)
    _denied_everywhere(tmp_path, mode, _GWS_CONFIG / "token_cache.json")


def test_a_credential_file_in_the_home_or_workspace_denies_the_file_not_its_parent(configured):
    """Denying the directory of a file kept loose in the home (or the workspace,
    or /) would deny the job everything under it, its own worktree included."""
    configured(
        GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=str(HOME / "gws-credentials.json"),
        GENUS_VAULT_KEY_FILE=str(WS / "master.key"),
        GENUS_FINGERPRINT_SALT_FILE="/salt",
    )
    found = secret_paths(HOME, WS)

    assert str(HOME / "gws-credentials.json") in found
    assert str(WS / "master.key") in found
    assert "/salt" in found
    for parent in (str(HOME), str(WS), "/"):
        assert parent not in found, parent


@pytest.mark.parametrize("mode", MODES)
def test_every_credential_file_setting_is_denied(tmp_path, configured, mode):
    """The vault master key, the fingerprint salt and the secrets env files,
    wherever the instance puts them."""
    files = {
        "GENUS_VAULT_KEY_FILE": "/opt/keys/vault/master.key",
        "GENUS_FINGERPRINT_SALT_FILE": "/opt/keys/salt/fingerprint",
        "ROBOTHOR_ENV_FILE": "/opt/instance/conf/robothor.env",
        "ROBOTHOR_SECRETS_BACKEND_FILE": "/opt/instance/in/secrets.env",
        "ROBOTHOR_SECRETS_FILE": "/opt/instance/out/secrets.env",
        "ROBOTHOR_INSTANCE_ENV": "/opt/instance/cron/robothor.env",
    }
    configured(**files)
    for value in files.values():
        _denied_everywhere(tmp_path, mode, Path(value))


def test_the_workspace_vault_key_and_salt_are_denied_by_default(configured):
    """With no override the vault master key and the fingerprint salt live at
    the workspace root, not under any directory already denied."""
    found = secret_paths(HOME, WS)
    assert str(WS / ".vault-key") in found
    assert str(WS / ".fingerprint-salt") in found


def test_a_credential_file_directly_under_a_top_level_directory_denies_only_the_file(configured):
    """``/etc/x.json`` must not deny all of ``/etc`` (CA certs, passwd, gitconfig)
    nor ``/tmp/x.json`` all of ``/tmp``."""
    configured(
        GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE="/etc/gws-credentials.json",
        GOOGLE_WORKSPACE_CLI_CONFIG_DIR="/tmp",
    )
    found = secret_paths(HOME, WS)

    assert "/etc/gws-credentials.json" in found
    assert "/etc" not in found
    assert "/tmp" not in found


@pytest.mark.parametrize("mode", MODES)
def test_a_credential_directory_holding_the_jobs_own_worktree_is_not_denied_whole(
    tmp_path, configured, mode
):
    """Deny beats the worktree's Edit/Write allow: a credential file beside the
    coding workspace must not take the job's own worktree away."""
    paths = _paths(tmp_path)
    configured(
        GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=str(tmp_path / "gws.json"),
        GOOGLE_WORKSPACE_CLI_CONFIG_DIR=str(tmp_path / "wt"),
    )
    deny_read = build_sandbox_settings(mode, paths, allowed_domains=())["sandbox"]["filesystem"][
        "denyRead"
    ]
    _, denied = build_permissions(mode, paths)

    for target in (paths.worktree, paths.git_common_dir):
        assert not _covers(deny_read, target), (mode, target)
        assert not any(
            _covers([r[len(t) + 2 : -4]], target)
            for t in ("Read", "Edit", "Write")
            for r in denied
            if r.startswith(f"{t}(/") and r.endswith("/**)")
        ), (mode, target)
    assert str(tmp_path / "gws.json") in deny_read


def test_a_credential_directory_holding_the_coding_workspace_is_not_denied_whole(
    tmp_path, configured, monkeypatch
):
    monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", str(tmp_path / "srv" / "coding"))
    configured(GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=str(tmp_path / "srv" / "gws.json"))
    found = secret_paths(HOME, WS)

    assert str(tmp_path / "srv" / "gws.json") in found
    assert str(tmp_path / "srv") not in found
