"""The sandbox playground is the only sandbox policy: nothing switches it off.

A shipped feature is the only code path, so there is no environment variable
that restores the old fail-the-run behaviour or lets a member's `exec` through.
These tests set the name it once had to a false value and check that the
refusals and the owner's host fallback still hold.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine.sandbox import RuntimeStatus
from robothor.engine.tests.test_runner_sandbox_playground import _execute

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Built, not written out, so this file is not itself a mention.
OLD_FLAG = "ROBOTHOR_SANDBOX_" + "PLAYGROUND"
ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


class _Cfg:
    id = "example-agent"
    sandbox = "docker"
    tools_allowed = ["exec"]
    tools_denied: list[str] = []


class _Ctx:
    def __init__(self, run_id: str) -> None:
        self.workspace = str(Path.cwd())
        self.agent_id = "example-agent"
        self.run_id = run_id
        self.tenant_id = "default"


@pytest.fixture(autouse=True)
def _old_flag_says_off(monkeypatch):
    monkeypatch.setenv(OLD_FLAG, "0")
    monkeypatch.setattr(sp, "_record", lambda *_a, **_k: None)


def test_no_tracked_file_mentions_the_old_flag() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    hits = []
    for name in filter(None, tracked):
        path = REPO_ROOT / name.decode()
        if path.suffix not in {".py", ".md", ".yaml", ".yml", ".ts", ".tsx", ".json"}:
            continue
        try:
            if OLD_FLAG in path.read_text(encoding="utf-8"):
                hits.append(name.decode())
        except (OSError, UnicodeDecodeError):
            continue
    assert hits == [], f"{OLD_FLAG} is still mentioned in {hits}"


async def test_a_members_exec_is_refused_with_the_old_flag_off() -> None:
    from robothor.engine.tools.handlers.filesystem import HANDLERS

    plan = sp.resolve_execution_plan(
        _Cfg(), "off", agent_id="example-agent", user_role="member", runtime=ABSENT
    )
    assert plan.where == "refuse"
    sp.set_exec_plan("run-member-no-flag", plan)
    result = await HANDLERS["exec"]({"command": "echo hi"}, _Ctx("run-member-no-flag"))
    assert result.get("error_type") == "exec_unavailable"
    assert "exit_code" not in result, "nothing may run"


@pytest.mark.asyncio
async def test_an_owners_person_driven_run_falls_back_to_the_host(engine_config, mock_db):
    run, _events, _ = await _execute(engine_config, runtime=ABSENT, start_error=None, role="owner")
    assert run.status.value == "completed", run.error_message
    plan = sp.exec_plan_for(run.id)
    assert plan is not None and plan.where == "host" and plan.degraded
