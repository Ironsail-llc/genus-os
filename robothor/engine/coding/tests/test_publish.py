"""claude_code_repo_prepare and claude_code_publish, as an agent calls them.

The remote is a local bare repository and GitHub is a stub HTTP server on
127.0.0.1 that records every request, so the whole path — clone, a verified
job, push, draft pull request — runs with no network.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from robothor.engine.coding import jobs as jobs_mod
from robothor.engine.coding.jobs import CodingJobManager, MemoryJobStore
from robothor.engine.coding.tests.conftest import git
from robothor.engine.coding.tests.test_jobs import VERIFY, FakeRunner, _commit_ok, _nothing
from robothor.engine.coding.tests.test_repos import TOKEN, _bare_remote
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import claude_code

NEW_TOOLS = ("claude_code_repo_prepare", "claude_code_publish")
PR_URL = "https://github.example/example/demo/pull/7"


class _Stub(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]]

    def _record(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        entry = {
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": json.loads(raw) if raw else None,
        }
        self.server.requests.append(entry)  # type: ignore[attr-defined]
        return entry

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        entry = self._record()
        if entry["path"] == "/repos/example/demo/pulls":
            self._reply(201, {"number": 7, "html_url": PR_URL, "draft": True})
        else:
            self._reply(404, {"message": "Not Found"})

    def do_PUT(self) -> None:  # noqa: N802
        self._record()
        self._reply(405, {"message": "not allowed"})

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._reply(404, {"message": "Not Found"})

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def github(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from robothor.engine.coding import publish

    monkeypatch.setattr(publish, "GITHUB_API", f"http://127.0.0.1:{server.server_port}")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def remote(tmp_path, monkeypatch, coding_env):
    url = _bare_remote(tmp_path)
    monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", str(tmp_path / "coding"))
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", url)
    # The fake runner commits with this process's git, in a fresh clone with no
    # user config: a CI runner has no global identity to fall back on.
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Alice Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "agent@example.com")
    from robothor.engine.coding import repos

    monkeypatch.setattr(repos, "github_token", lambda tenant_id: TOKEN)
    return url


@pytest.fixture
def audit(monkeypatch):
    events: list[dict[str, Any]] = []

    def record(event_type, action, **kw):
        events.append({"event_type": event_type, "action": action, **kw})
        return {"id": len(events), "timestamp": "now"}

    monkeypatch.setattr("robothor.audit.logger.log_event", record)
    return events


def _manager(*rounds):
    mgr = CodingJobManager(
        store=MemoryJobStore(),
        runner=FakeRunner(*(rounds or (_commit_ok,))),
        token_resolver=lambda t: "tok",
    )
    jobs_mod.use_manager(mgr)
    return mgr


@pytest.fixture
def manager(remote):
    mgr = _manager()
    yield mgr
    jobs_mod.use_manager(None)


def _ctx(**kw) -> ToolContext:
    base = {"agent_id": "main", "run_id": "", "tenant_id": "test-tenant", "user_role": "owner"}
    base.update(kw)
    return ToolContext(**base)


async def _call(name: str, args: dict[str, Any], ctx: ToolContext | None = None) -> dict:
    return await claude_code.HANDLERS[name](args, ctx or _ctx())


async def _done_job(remote_url: str) -> dict[str, Any]:
    prepared = await _call("claude_code_repo_prepare", {"repo": remote_url})
    assert "error" not in prepared, prepared
    started = await _call(
        "claude_code_start",
        {
            "task": "make ok.txt exist",
            "repo_path": prepared["repo_path"],
            "acceptance": {"verify_command": VERIFY, "require_commit": True},
        },
    )
    assert "error" not in started, started
    waited = await _call("claude_code_wait", {"job_id": started["job_id"], "timeout_s": 20})
    assert waited["status"] == "done", waited
    return waited


def _remote_branches(remote_url: str) -> dict[str, str]:
    bare = Path(remote_url.removeprefix("file://"))
    out = git(bare, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


# ── registration ──────────────────────────────────────────────────────


def test_the_new_tools_are_registered_opt_in_and_benchmark_gated():
    from robothor.engine.benchmark_sandbox import EXTERNAL_SIDE_EFFECT_TOOLS
    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS, OPT_IN_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    schemas, handlers = get_engine_schemas(), builtin_handlers()
    for name in NEW_TOOLS:
        assert name in schemas and name in handlers, name
    assert set(NEW_TOOLS) <= CLAUDE_CODE_TOOLS
    assert set(NEW_TOOLS) <= OPT_IN_TOOLS
    assert set(NEW_TOOLS) <= EXTERNAL_SIDE_EFFECT_TOOLS


def test_there_is_no_merge_tool():
    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS

    assert not [t for t in CLAUDE_CODE_TOOLS if "merge" in t]


def test_the_main_template_opts_in_to_prepare_and_publish():
    import yaml

    root = Path(__file__).resolve().parents[4]
    text = (root / "templates/agents/core/main/manifest.template.yaml").read_text()
    manifest = yaml.safe_load(text.replace("{{ timezone }}", "UTC"))
    assert set(NEW_TOOLS) <= set(manifest["tools_opt_in"])


@pytest.mark.parametrize("name", NEW_TOOLS)
async def test_benchmark_runs_are_refused(name):
    out = await _call(name, {"repo": "example/demo", "job_id": "x"}, _ctx(is_benchmark=True))
    assert out.get("guard") == "is_benchmark"


# ── prepare ───────────────────────────────────────────────────────────


async def test_prepare_without_a_workspace_names_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_CODING_WORKSPACE", raising=False)
    out = await _call("claude_code_repo_prepare", {"repo": "example/demo"})
    assert "ROBOTHOR_CODING_WORKSPACE" in out["error"]


async def test_prepare_clones_then_fetches(remote, tmp_path):
    first = await _call("claude_code_repo_prepare", {"repo": remote})
    assert first["cloned"] is True
    assert first["repo_path"] == str((tmp_path / "coding" / "example" / "demo").resolve())
    assert first["default_branch"] == "main"
    assert first["next"].startswith("claude_code_start")
    second = await _call("claude_code_repo_prepare", {"repo": remote})
    assert second["cloned"] is False
    assert TOKEN not in json.dumps(first) + json.dumps(second)


async def test_prepare_refuses_a_remote_outside_the_allowlist(remote, tmp_path):
    other = _bare_remote(tmp_path, owner="someone", name="else")
    out = await _call("claude_code_repo_prepare", {"repo": other})
    assert "ROBOTHOR_CODING_REMOTES" in out["error"]
    assert not (tmp_path / "coding" / "someone").exists()


# ── progress ──────────────────────────────────────────────────────────


async def test_job_progress_reaches_the_starting_runs_status_sink(manager, remote):
    from robothor.engine.run_status import register_status_sink, unregister_status_sink

    seen: list[dict[str, Any]] = []

    async def sink(event: dict[str, Any]) -> None:
        seen.append(event)

    register_status_sink("run-1", sink)
    try:
        prepared = await _call("claude_code_repo_prepare", {"repo": remote}, _ctx(run_id="run-1"))
        started = await _call(
            "claude_code_start",
            {
                "task": "make ok.txt exist",
                "repo_path": prepared["repo_path"],
                "acceptance": {"verify_command": VERIFY},
            },
            _ctx(run_id="run-1"),
        )
        await _call(
            "claude_code_wait", {"job_id": started["job_id"], "timeout_s": 20}, _ctx(run_id="run-1")
        )
    finally:
        unregister_status_sink("run-1")
    progress = [e for e in seen if e.get("event") == "coding_progress"]
    assert len(progress) >= 1
    assert progress[0]["job_id"] == started["job_id"]
    assert "pytest" in progress[0]["line"]


# ── publish ───────────────────────────────────────────────────────────


async def test_publish_pushes_the_branch_and_opens_one_draft_pr(manager, remote, github, audit):
    job = await _done_job(remote)
    out = await _call(
        "claude_code_publish",
        {"job_id": job["job_id"], "title": "Add ok.txt", "body": "Verified.", "ticket": "ABC-12"},
    )
    assert "error" not in out, out
    assert out["pr_url"] == PR_URL
    assert out["draft"] is True
    assert out["branch"] == job["branch"]

    assert _remote_branches(remote).get(job["branch"]) == job["commit_sha"]

    posts = [r for r in github.requests if r["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/repos/example/demo/pulls"
    body = posts[0]["body"]
    assert body["draft"] is True
    assert body["head"] == job["branch"]
    assert body["base"] == "main"
    assert body["title"] == "Add ok.txt (ABC-12)"
    assert posts[0]["auth"] == f"Bearer {TOKEN}"
    assert not [r for r in github.requests if "merge" in r["path"] or r["method"] == "PUT"]

    assert TOKEN not in json.dumps(out)
    assert [e["status"] for e in audit if e["action"] == "claude_code_publish"] == ["ok"]


@pytest.mark.parametrize("role", ["member", "user", "service", ""])
async def test_publish_is_refused_below_owner_and_audited(manager, remote, github, audit, role):
    job = await _done_job(remote)
    out = await _call(
        "claude_code_publish", {"job_id": job["job_id"], "title": "t"}, _ctx(user_role=role)
    )
    assert "error" in out
    assert (role or "no role") in out["error"]
    assert github.requests == []
    assert not [b for b in _remote_branches(remote) if b.startswith("genus/")]
    denied = [e for e in audit if e["action"] == "claude_code_publish"]
    assert len(denied) == 1 and denied[0]["status"] == "denied"


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_may_publish(manager, remote, github, audit, role):
    job = await _done_job(remote)
    out = await _call(
        "claude_code_publish", {"job_id": job["job_id"], "title": "t"}, _ctx(user_role=role)
    )
    assert out.get("pr_url") == PR_URL, out


async def test_publish_refuses_an_unfinished_or_unverified_job(remote, github):
    mgr = _manager(_nothing, _nothing)  # never commits: fails acceptance
    try:
        prepared = await _call("claude_code_repo_prepare", {"repo": remote})
        started = await _call(
            "claude_code_start",
            {
                "task": "t",
                "repo_path": prepared["repo_path"],
                "acceptance": {"verify_command": VERIFY},
                "max_rounds": 2,
            },
        )
        await mgr.wait(started["job_id"], "test-tenant", timeout_s=20)
        out = await _call("claude_code_publish", {"job_id": started["job_id"], "title": "t"})
    finally:
        jobs_mod.use_manager(None)
    assert "done" in out["error"]
    assert github.requests == []


async def test_publish_of_an_unknown_job_is_not_found(manager, remote, github):
    out = await _call("claude_code_publish", {"job_id": "nope", "title": "t"})
    assert "not found" in out["error"]


async def test_publish_needs_a_title(manager, remote, github):
    job = await _done_job(remote)
    out = await _call("claude_code_publish", {"job_id": job["job_id"]})
    assert "title" in out["error"]


async def test_publish_refuses_a_repo_whose_origin_is_not_allowlisted(
    manager, remote, github, git_repo
):
    started = await _call(
        "claude_code_start",
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}},
    )
    waited = await _call("claude_code_wait", {"job_id": started["job_id"], "timeout_s": 20})
    assert waited["status"] == "done", waited
    out = await _call("claude_code_publish", {"job_id": started["job_id"], "title": "t"})
    assert "ROBOTHOR_CODING_REMOTES" in out["error"]
    assert github.requests == []


async def test_publish_needs_a_github_token(manager, remote, github, monkeypatch):
    job = await _done_job(remote)
    from robothor.engine.coding import repos

    monkeypatch.setattr(repos, "github_token", lambda tenant_id: "")
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert "GITHUB_TOKEN" in out["error"]
    assert github.requests == []


async def test_the_token_never_reaches_logs(manager, remote, github, audit, caplog):
    caplog.set_level("DEBUG")
    job = await _done_job(remote)
    await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(audit)


# ── review round 1 ────────────────────────────────────────────────────


async def test_publish_runs_no_pre_push_hook(manager, remote, github, audit, tmp_path):
    job = await _done_job(remote)
    clone = tmp_path / "coding" / "example" / "demo"
    marker = tmp_path / "pre-push-ran"
    hook = clone / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\nenv > {marker}\nexit 1\n")
    hook.chmod(0o755)
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert out.get("pr_url") == PR_URL, out
    assert not marker.exists()


async def test_publish_refuses_a_remote_that_is_not_on_github(
    manager, remote, github, audit, monkeypatch
):
    """The token and the PR API are GitHub's: no push to another host, no network."""
    job = await _done_job(remote)
    other = "https://git.example.org/example/demo.git"
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", f"{remote},{other}")
    from robothor.settings import reset_settings

    reset_settings()
    repo = Path((await _call("claude_code_repo_prepare", {"repo": remote}))["repo_path"])
    git(repo, "remote", "set-url", "origin", other)
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert "GitHub" in out["error"]
    assert github.requests == []


# ── QA round 1 ────────────────────────────────────────────────────────


def test_a_claude_code_deny_pattern_covers_every_tool_of_the_family():
    """``tools_denied: [claude_code_*]`` and the RBAC rows written that way must deny them all."""
    from fnmatch import fnmatch

    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS

    assert [t for t in CLAUDE_CODE_TOOLS if not fnmatch(t, "claude_code_*")] == []


def _denials(audit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in audit if e["action"] == "claude_code_publish" and e["status"] == "denied"]


async def test_every_publish_refusal_is_audited_with_its_reason(
    manager, remote, github, audit, monkeypatch, git_repo
):
    from robothor.engine.coding import repos

    async def publish(args: dict[str, Any]) -> dict[str, Any]:
        before = len(_denials(audit))
        out = await _call("claude_code_publish", args)
        assert "error" in out, out
        new = _denials(audit)[before:]
        assert len(new) == 1, (args, audit)
        assert new[0]["details"]["reason"], new[0]
        return new[0]

    assert "title" in (await publish({"job_id": "nope"}))["details"]["reason"]
    assert "not found" in (await publish({"job_id": "nope", "title": "t"}))["details"]["reason"]

    job = await _done_job(remote)

    monkeypatch.setattr(repos, "github_token", lambda tenant_id: "")
    assert (
        "GITHUB_TOKEN"
        in (await publish({"job_id": job["job_id"], "title": "t"}))["details"]["reason"]
    )

    _manager(_commit_ok)  # the fixture's runner spent its one committing round above
    started = await _call(
        "claude_code_start",
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}},
    )
    waited = await _call("claude_code_wait", {"job_id": started["job_id"], "timeout_s": 20})
    assert waited["status"] == "done", waited
    stray = await publish({"job_id": started["job_id"], "title": "t"})
    assert "ROBOTHOR_CODING_REMOTES" in stray["details"]["reason"]
    assert github.requests == []


async def test_an_unverified_job_refusal_is_audited(remote, github, audit):
    mgr = _manager(_nothing, _nothing)
    try:
        prepared = await _call("claude_code_repo_prepare", {"repo": remote})
        started = await _call(
            "claude_code_start",
            {
                "task": "t",
                "repo_path": prepared["repo_path"],
                "acceptance": {"verify_command": VERIFY},
                "max_rounds": 1,
            },
        )
        await mgr.wait(started["job_id"], "test-tenant", timeout_s=20)
        await _call("claude_code_publish", {"job_id": started["job_id"], "title": "t"})
    finally:
        jobs_mod.use_manager(None)
    denied = _denials(audit)
    assert len(denied) == 1 and "done" in denied[0]["details"]["reason"]


# ── review round 4 ────────────────────────────────────────────────────


def _move_branch(repo: Path, branch: str) -> str:
    """What another job's shell can do: point the job's branch at an unverified commit."""
    tree = git(repo, "rev-parse", f"refs/heads/{branch}^{{tree}}")
    parent = git(repo, "rev-parse", f"refs/heads/{branch}")
    moved = git(repo, "commit-tree", tree, "-p", parent, "-m", "unverified")
    git(repo, "update-ref", f"refs/heads/{branch}", moved)
    return moved


async def test_publish_refuses_a_branch_that_moved_after_verify(
    manager, remote, github, audit, tmp_path
):
    job = await _done_job(remote)
    clone = tmp_path / "coding" / "example" / "demo"
    moved = _move_branch(clone, job["branch"])
    assert moved != job["commit_sha"]

    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})

    assert "error" in out, out
    assert "moved" in out["error"]
    assert job["commit_sha"] in out["error"]
    assert job["branch"] not in _remote_branches(remote)
    assert moved not in _remote_branches(remote).values()
    assert github.requests == []
    statuses = [e["status"] for e in audit if e["action"] == "claude_code_publish"]
    assert statuses == ["error"]


async def test_publish_pushes_the_verified_sha_itself(manager, remote, github, monkeypatch):
    """The refspec names the verified commit, not the branch whose tip could change."""
    from robothor.engine.coding import repos

    calls: list[tuple[str, ...]] = []
    real = repos.git

    async def spy(cwd, *args, **kw):
        calls.append(args)
        return await real(cwd, *args, **kw)

    monkeypatch.setattr(repos, "git", spy)
    job = await _done_job(remote)
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert out.get("pr_url") == PR_URL, out
    pushes = [a for a in calls if a and a[0] == "push"]
    assert len(pushes) == 1
    assert f"{job['commit_sha']}:refs/heads/{job['branch']}" in pushes[0]


@pytest.mark.parametrize(
    "setup",
    [
        "pushurl",  # fetch from the allowlisted repo, push somewhere else
        "second-url",  # a second remote.origin.url: git pushes to every one
        "push-instead-of",  # a rewrite of the allowlisted URL for pushes only
    ],
)
async def test_publish_checks_every_push_url_against_the_allowlist(
    manager, remote, github, audit, tmp_path, setup
):
    from robothor.engine.coding.tests.test_repos import _bare_remote as make_bare

    job = await _done_job(remote)
    clone = tmp_path / "coding" / "example" / "demo"
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    other = make_bare(other_root, owner="other")
    if setup == "pushurl":
        git(clone, "config", "remote.origin.pushurl", other)
    elif setup == "second-url":
        git(clone, "config", "--add", "remote.origin.url", other)
    else:
        git(clone, "config", f"url.{other}.pushInsteadOf", remote)

    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})

    assert "error" in out, out
    assert "ROBOTHOR_CODING_REMOTES" in out["error"]
    assert job["branch"] not in _remote_branches(other)
    assert job["branch"] not in _remote_branches(remote)
    assert github.requests == []


# ── no switch: publishing is bounded, not gated ───────────────────────

# A retired boolean once gated publishing. Whether it is absent or set to "0",
# publish works, and every bound on it holds.
STALE_SWITCH = "ROBOTHOR_CODING_PR_ENABLED"


@pytest.fixture(params=["absent", "0"])
def stale_switch(request, monkeypatch):
    from robothor.settings import reset_settings

    if request.param == "absent":
        monkeypatch.delenv(STALE_SWITCH, raising=False)
    else:
        monkeypatch.setenv(STALE_SWITCH, request.param)
    reset_settings()
    return request.param


def test_there_is_no_setting_that_switches_publishing_off():
    from robothor.settings.model import CodingSettings

    assert "pr_enabled" not in CodingSettings.model_fields
    root = Path(__file__).resolve().parents[4]
    assert STALE_SWITCH not in (root / "docs/reference/configuration.md").read_text()


async def test_publish_opens_a_draft_pr_with_no_switch_set(
    manager, remote, github, audit, stale_switch
):
    job = await _done_job(remote)
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert out.get("pr_url") == PR_URL, out
    assert _remote_branches(remote).get(job["branch"]) == job["commit_sha"]
    assert [e["status"] for e in audit if e["action"] == "claude_code_publish"] == ["ok"]


async def test_no_refusal_names_the_retired_switch(manager, remote, github, audit, stale_switch):
    job = await _done_job(remote)
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert STALE_SWITCH not in json.dumps(out) + json.dumps(audit)


async def test_a_push_outside_the_allowlist_is_refused_with_no_switch_set(
    manager, remote, github, audit, tmp_path, stale_switch
):
    from robothor.engine.coding.tests.test_repos import _bare_remote as make_bare

    job = await _done_job(remote)
    clone = tmp_path / "coding" / "example" / "demo"
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    other = make_bare(other_root, owner="other")
    git(clone, "config", "remote.origin.pushurl", other)

    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})

    assert "ROBOTHOR_CODING_REMOTES" in out.get("error", ""), out
    assert job["branch"] not in _remote_branches(other)
    assert job["branch"] not in _remote_branches(remote)
    assert github.requests == []
    assert [e["status"] for e in audit if e["action"] == "claude_code_publish"] == ["denied"]


async def test_an_empty_allowlist_refuses_every_publish_with_no_switch_set(
    manager, remote, github, audit, monkeypatch, stale_switch
):
    """Without ROBOTHOR_CODING_REMOTES there is nowhere to push: the only off state."""
    from robothor.settings import reset_settings

    job = await _done_job(remote)
    monkeypatch.setenv("ROBOTHOR_CODING_REMOTES", "")
    reset_settings()
    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})
    assert "ROBOTHOR_CODING_REMOTES" in out.get("error", ""), out
    assert job["branch"] not in _remote_branches(remote)
    assert github.requests == []


async def test_an_unverified_sha_is_refused_with_no_switch_set(
    manager, remote, github, audit, tmp_path, stale_switch
):
    job = await _done_job(remote)
    clone = tmp_path / "coding" / "example" / "demo"
    moved = _move_branch(clone, job["branch"])

    out = await _call("claude_code_publish", {"job_id": job["job_id"], "title": "t"})

    assert "moved" in out.get("error", ""), out
    assert moved not in _remote_branches(remote).values()
    assert job["branch"] not in _remote_branches(remote)
    assert github.requests == []


async def test_an_unverified_job_is_refused_with_no_switch_set(remote, github, stale_switch):
    mgr = _manager(_nothing, _nothing)
    try:
        prepared = await _call("claude_code_repo_prepare", {"repo": remote})
        started = await _call(
            "claude_code_start",
            {
                "task": "t",
                "repo_path": prepared["repo_path"],
                "acceptance": {"verify_command": VERIFY},
                "max_rounds": 1,
            },
        )
        await mgr.wait(started["job_id"], "test-tenant", timeout_s=20)
        out = await _call("claude_code_publish", {"job_id": started["job_id"], "title": "t"})
    finally:
        jobs_mod.use_manager(None)
    assert "done" in out.get("error", ""), out
    assert github.requests == []


@pytest.mark.parametrize("role", ["member", "user", "service", ""])
async def test_rbac_holds_with_no_switch_set(manager, remote, github, stale_switch, role):
    job = await _done_job(remote)
    out = await _call(
        "claude_code_publish", {"job_id": job["job_id"], "title": "t"}, _ctx(user_role=role)
    )
    assert (role or "no role") in out.get("error", ""), out
    assert github.requests == []
