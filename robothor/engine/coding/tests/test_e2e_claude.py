"""End to end: a real ``claude -p`` makes a failing test pass and commits.

Spends real money (cents, with a cheap model). Runs only when a Claude Code
token resolves (``robothor claude-code login``, or CLAUDE_CODE_OAUTH_TOKEN in
the environment) or ``ROBOTHOR_E2E_CLAUDE_AUTH=host`` says to run the job with
``ROBOTHOR_CLAUDE_CODE_AUTH=host`` (the developer's own Claude Code login).
Everything happens in a throwaway repository under tmp_path.

    ROBOTHOR_E2E_CLAUDE_AUTH=host pytest -m llm robothor/engine/coding/tests/test_e2e_claude.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from robothor.engine.coding.env import resolve_oauth_token
from robothor.engine.coding.jobs import Acceptance, CodingJobManager, JobStatus, MemoryJobStore
from robothor.engine.coding.runner import ClaudeCodeError, resolve_claude_binary
from robothor.engine.coding.tests.conftest import git
from robothor.engine.session_goal import GoalEvidence, validate_evidence

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.llm, pytest.mark.slow, pytest.mark.timeout(900)]


def _host_auth() -> bool:
    return os.environ.get("ROBOTHOR_E2E_CLAUDE_AUTH", "").strip().lower() == "host"


def _auth_available() -> bool:
    if _host_auth():
        return True
    try:
        return bool(resolve_oauth_token())
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def failing_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "calc"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Alice Example")
    git(repo, "config", "user.email", "agent@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "calc with a bug")
    return repo


async def test_claude_code_makes_the_failing_test_pass_and_commits(
    failing_repo, coding_env, monkeypatch
):
    try:
        resolve_claude_binary()
    except ClaudeCodeError:
        pytest.skip("Claude Code CLI not installed")
    if not _auth_available():
        pytest.skip("no Claude Code token (robothor claude-code login) and not host auth")

    if _host_auth():
        monkeypatch.setenv("ROBOTHOR_CLAUDE_CODE_AUTH", "host")
        from robothor.settings import reset_settings

        reset_settings()

    verify = f"{sys.executable} -m pytest -q -p no:cacheprovider test_calc.py"
    assert subprocess.run(verify.split(), cwd=failing_repo, capture_output=True).returncode != 0

    mgr = CodingJobManager(store=MemoryJobStore())
    job = await mgr.start(
        tenant_id="test-tenant",
        agent_id="main",
        task="The test in test_calc.py fails. Fix the bug in calc.py so it passes, then commit.",
        repo_path=str(failing_repo),
        acceptance=Acceptance(verify_command=verify, require_commit=True),
        model=os.environ.get("ROBOTHOR_E2E_CLAUDE_MODEL", "haiku"),
        max_budget_usd=1.0,
        max_rounds=2,
    )
    job = await mgr.wait(job.id, "test-tenant", timeout_s=840)

    print(
        f"\nE2E: status={job.status} rounds={job.rounds} cost=${job.cost_usd:.4f} error={job.error!r}"
    )
    assert job.status == JobStatus.DONE, (
        job.error,
        job.events_tail[-10:],
        job.result.get("last_round"),
    )
    assert job.result["verify"]["passed"] is True
    sha = job.result["commit_sha"]
    assert sha and git(failing_repo, "rev-parse", job.branch) == sha
    assert "a + b" in git(failing_repo, "show", f"{sha}:calc.py")
    for raw in job.evidence:
        item = GoalEvidence(kind=raw["kind"], summary=raw["summary"], reference=raw["reference"])
        ok, why = validate_evidence(item, workspace=failing_repo)
        assert ok, why
