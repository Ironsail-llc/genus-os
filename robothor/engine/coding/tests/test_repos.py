"""Putting a repository on disk for a coding job: the allowlist, clone, fetch.

Every remote here is a local bare repository reached through a ``file://``
URL, so nothing touches the network.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from robothor.engine.coding import repos
from robothor.engine.coding.tests.conftest import git

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "ghs_exampletoken0123456789"


def _bare_remote(tmp_path: Path, owner: str = "example", name: str = "demo") -> str:
    """A bare repository with one commit on main; returns its file:// URL."""
    bare = tmp_path / "remotes" / owner / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / f"seed-{owner}-{name}"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "config", "user.name", "Alice Example")
    git(seed, "config", "user.email", "agent@example.com")
    git(seed, "config", "commit.gpgsign", "false")
    (seed / "README.md").write_text("# demo\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return bare.as_uri()


def _push_new_commit(tmp_path: Path, owner: str = "example", name: str = "demo") -> str:
    seed = tmp_path / f"seed-{owner}-{name}"
    (seed / "CHANGES.md").write_text("more\n")
    git(seed, "add", "CHANGES.md")
    git(seed, "commit", "-q", "-m", "more")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return git(seed, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "coding"
    monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", str(ws))
    return ws


@pytest.fixture
def remote(tmp_path, monkeypatch):
    url = _bare_remote(tmp_path)
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", url)
    return url


# ── the allowlist ─────────────────────────────────────────────────────


def test_shorthand_names_a_github_repository():
    spec = repos.parse_remote("example/demo")
    assert (spec.owner, spec.name) == ("example", "demo")
    assert spec.url == "https://github.com/example/demo.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/demo",
        "https://github.com/example/demo.git",
        "file:///srv/remotes/example/demo.git",
    ],
)
def test_a_url_names_its_last_two_path_segments(url):
    spec = repos.parse_remote(url)
    assert (spec.owner, spec.name) == ("example", "demo")


@pytest.mark.parametrize(
    "bad", ["", "demo", "-x/demo", "example/../etc", "ext::sh -c id", ".home/demo", "x/.git"]
)
def test_malformed_remotes_are_refused(bad):
    with pytest.raises(repos.RepoError):
        repos.parse_remote(bad)


async def test_no_workspace_refuses_and_names_the_setting(remote, tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_CODING_WORKSPACE", raising=False)
    with pytest.raises(repos.RepoError, match="ROBOTHOR_CODING_WORKSPACE"):
        await repos.ensure_repo(remote, token="")


async def test_an_empty_allowlist_refuses_and_names_the_setting(workspace, tmp_path, monkeypatch):
    url = _bare_remote(tmp_path)
    monkeypatch.delenv("ROBOTHOR_CODING_REMOTES", raising=False)
    with pytest.raises(repos.RepoError, match="ROBOTHOR_CODING_REMOTES"):
        await repos.ensure_repo(url, token="")
    assert not workspace.exists() or not any(workspace.iterdir())


async def test_a_remote_outside_the_allowlist_is_refused(workspace, remote, tmp_path):
    other = _bare_remote(tmp_path, owner="someone", name="else")
    with pytest.raises(repos.RepoError, match="ROBOTHOR_CODING_REMOTES"):
        await repos.ensure_repo(other, token="")
    assert not (workspace / "someone").exists()


def test_shorthand_and_url_entries_match_the_same_repository(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", "example/demo, other/thing")
    assert repos.allowed_remote("https://github.com/example/demo.git").name == "demo"
    assert repos.allowed_remote("example/demo").url == "https://github.com/example/demo.git"
    with pytest.raises(repos.RepoError):
        repos.allowed_remote("example/other")


# ── clone once, fetch after ───────────────────────────────────────────


async def test_first_call_clones_and_the_second_fetches(workspace, remote, tmp_path):
    first = await repos.ensure_repo(remote, token="")
    assert first.cloned is True
    assert first.path == workspace / "example" / "demo"
    assert (first.path / "README.md").is_file()
    assert first.default_branch == "main"

    new_sha = _push_new_commit(tmp_path)
    second = await repos.ensure_repo(remote, token="")
    assert second.cloned is False
    assert second.path == first.path
    assert second.head_sha == new_sha
    assert (second.path / "CHANGES.md").is_file()


async def test_concurrent_prepares_clone_once(workspace, remote):
    results = await asyncio.gather(*(repos.ensure_repo(remote, token="") for _ in range(4)))
    assert sum(1 for r in results if r.cloned) == 1
    assert len({r.path for r in results}) == 1


async def test_the_clone_is_an_implicit_repo_root(workspace, remote, monkeypatch):
    from robothor.engine.coding.jobs import check_repo_path

    monkeypatch.delenv("ROBOTHOR_CODING_REPO_ROOTS", raising=False)
    prepared = await repos.ensure_repo(remote, token="")
    assert check_repo_path(str(prepared.path)) == prepared.path.resolve()


# ── the token ─────────────────────────────────────────────────────────


def test_the_token_travels_as_an_https_header_in_the_environment():
    env = repos.git_env("https://github.com/example/demo.git", TOKEN)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    keys = {env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")}
    assert "http.https://github.com/.extraheader" in keys
    assert all(TOKEN not in v for v in env.values())  # base64 inside a header only


def test_no_header_is_sent_without_a_token_or_to_a_non_https_remote():
    assert "GIT_CONFIG_COUNT" not in repos.git_env("https://github.com/example/demo.git", "")
    assert "GIT_CONFIG_COUNT" not in repos.git_env("file:///srv/example/demo.git", TOKEN)


async def test_the_token_is_never_in_argv_or_the_clone_config(
    workspace, remote, tmp_path, monkeypatch, caplog
):
    argvs: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*argv, **kw):
        argvs.append(tuple(str(a) for a in argv))
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    caplog.set_level("DEBUG")
    prepared = await repos.ensure_repo(remote, token=TOKEN)
    _push_new_commit(tmp_path)
    await repos.ensure_repo(remote, token=TOKEN)

    assert argvs, "git was never run"
    assert all(TOKEN not in " ".join(a) for a in argvs)
    assert TOKEN not in (prepared.path / ".git" / "config").read_text()
    assert TOKEN not in caplog.text


async def test_a_failing_clone_reports_without_the_token(workspace, tmp_path, monkeypatch):
    missing = (tmp_path / "remotes" / "example" / "gone.git").as_uri()
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", missing)
    with pytest.raises(repos.RepoError) as err:
        await repos.ensure_repo(missing, token=TOKEN)
    assert "clone" in str(err.value)
    assert TOKEN not in str(err.value)
    assert not (workspace / "example" / "gone" / ".git").exists()


def test_redact_removes_the_token_and_its_basic_form():
    import base64

    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    text = f"fatal: {TOKEN} and {basic}"
    cleaned = repos.redact(text, TOKEN)
    assert TOKEN not in cleaned and basic not in cleaned


def test_shell_metacharacters_in_a_remote_are_refused():
    with pytest.raises(repos.RepoError):
        repos.parse_remote("https://github.com/example/demo;rm -rf ~")
