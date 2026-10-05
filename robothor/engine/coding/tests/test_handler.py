"""The five claude_code_* tools, as an agent calls them."""

from __future__ import annotations

import pytest

from robothor.engine.coding import jobs as jobs_mod
from robothor.engine.coding.jobs import CodingJobManager, MemoryJobStore
from robothor.engine.coding.tests.test_jobs import VERIFY, FakeRunner, _commit_ok, _nothing
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import claude_code

TOOLS = (
    "claude_code_start",
    "claude_code_status",
    "claude_code_wait",
    "claude_code_followup",
    "claude_code_cancel",
)


@pytest.fixture
def manager(coding_env):
    runner = FakeRunner(_nothing, _commit_ok)
    mgr = CodingJobManager(store=MemoryJobStore(), runner=runner, token_resolver=lambda t: "tok")
    jobs_mod.use_manager(mgr)
    yield mgr
    jobs_mod.use_manager(None)


def _ctx(**kw) -> ToolContext:
    base = {"agent_id": "main", "run_id": "", "tenant_id": "test-tenant"}
    base.update(kw)
    return ToolContext(**base)


def test_every_tool_is_registered_with_a_schema_and_a_handler():
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    schemas = get_engine_schemas()
    handlers = builtin_handlers()
    for name in TOOLS:
        assert name in schemas, name
        assert name in handlers, name


def test_the_tools_are_opt_in_and_never_benchmark_reachable():
    from robothor.engine.benchmark_sandbox import EXTERNAL_SIDE_EFFECT_TOOLS
    from robothor.engine.tools.constants import OPT_IN_TOOLS

    assert set(TOOLS) <= OPT_IN_TOOLS
    assert set(TOOLS) <= EXTERNAL_SIDE_EFFECT_TOOLS


@pytest.mark.parametrize("name", TOOLS)
async def test_benchmark_runs_are_refused(name, manager):
    out = await claude_code.HANDLERS[name]({"job_id": "x", "task": "t"}, _ctx(is_benchmark=True))
    assert out.get("guard") == "is_benchmark"


async def test_start_wait_status_roundtrip(git_repo, manager):
    started = await claude_code.HANDLERS["claude_code_start"](
        {
            "task": "make ok.txt exist",
            "repo_path": str(git_repo),
            "acceptance": {"verify_command": VERIFY, "require_commit": True},
        },
        _ctx(),
    )
    assert "error" not in started, started
    job_id = started["job_id"]

    waited = await claude_code.HANDLERS["claude_code_wait"](
        {"job_id": job_id, "timeout_s": 20}, _ctx()
    )
    assert waited["status"] == "done", waited
    assert waited["rounds"] == 2
    assert waited["commit_sha"]
    assert waited["verify"]["passed"] is True
    assert {e["kind"] for e in waited["evidence"]} == {"test_run", "commit"}
    import json

    assert len(json.dumps(waited)) < 4000  # fits the persisted step record

    status = await claude_code.HANDLERS["claude_code_status"]({"job_id": job_id}, _ctx())
    assert status["status"] == "done"
    assert status["job_id"] == job_id


async def test_another_tenant_gets_not_found(git_repo, manager):
    started = await claude_code.HANDLERS["claude_code_start"](
        {
            "task": "t",
            "repo_path": str(git_repo),
            "acceptance": {"verify_command": VERIFY},
        },
        _ctx(),
    )
    out = await claude_code.HANDLERS["claude_code_status"](
        {"job_id": started["job_id"]}, _ctx(tenant_id="other-tenant")
    )
    assert "not found" in out["error"]
    await manager.wait(started["job_id"], "test-tenant", timeout_s=20)


async def test_start_validates_its_arguments(git_repo, manager, tmp_path):
    start = claude_code.HANDLERS["claude_code_start"]

    assert "error" in await start(
        {"repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    assert "error" in await start({"task": "t", "acceptance": {"verify_command": VERIFY}}, _ctx())
    assert "error" in await start({"task": "t", "repo_path": str(git_repo)}, _ctx())
    out = await start(
        {
            "task": "t",
            "repo_path": str(git_repo),
            "acceptance": {"verify_command": VERIFY},
            "mode": "yolo",
        },
        _ctx(),
    )
    assert "mode" in out["error"]
    out = await start(
        {"task": "t", "repo_path": "relative/path", "acceptance": {"verify_command": VERIFY}},
        _ctx(),
    )
    assert "absolute" in out["error"]
    out = await start(
        {"task": "t", "repo_path": str(tmp_path), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    assert "git" in out["error"]


async def test_repo_roots_allowlist_is_enforced(git_repo, manager, monkeypatch, tmp_path):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_CODING_REPO_ROOTS", str(tmp_path / "elsewhere"))
    reset_settings()
    out = await claude_code.HANDLERS["claude_code_start"](
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    assert "ROBOTHOR_CODING_REPO_ROOTS" in out["error"]


async def test_wait_is_clamped_by_run_pacing(git_repo, manager, monkeypatch):
    seen = {}

    def fake_clamp(requested, **kw):
        seen["requested"] = requested
        return 1, "timeout clamped to 1s: the run has 31s left"

    monkeypatch.setattr("robothor.engine.run_pacing.clamp_tool_timeout", fake_clamp)
    started = await claude_code.HANDLERS["claude_code_start"](
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    out = await claude_code.HANDLERS["claude_code_wait"](
        {"job_id": started["job_id"], "timeout_s": 900}, _ctx()
    )
    assert seen["requested"] == 900
    assert out.get("timeout_note", "").startswith("timeout clamped")
    await manager.wait(started["job_id"], "test-tenant", timeout_s=20)


async def test_cancel_and_followup_route_to_the_manager(git_repo, manager):
    started = await claude_code.HANDLERS["claude_code_start"](
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    job_id = started["job_id"]
    await manager.wait(job_id, "test-tenant", timeout_s=20)

    out = await claude_code.HANDLERS["claude_code_followup"](
        {"job_id": job_id, "message": ""}, _ctx()
    )
    assert "error" in out
    out = await claude_code.HANDLERS["claude_code_cancel"]({"job_id": job_id}, _ctx())
    # A finished job stays finished; cancel says so rather than pretending.
    assert out["status"] == "done"


def test_the_main_template_opts_in_to_every_claude_code_tool():
    from pathlib import Path

    import yaml

    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS

    root = Path(__file__).resolve().parents[4]
    text = (root / "templates/agents/core/main/manifest.template.yaml").read_text()
    manifest = yaml.safe_load(text.replace("{{ timezone }}", "UTC"))

    assert "tools_allowed" not in manifest  # main keeps the default set...
    assert set(manifest["tools_opt_in"]) == CLAUDE_CODE_TOOLS  # ...plus these


async def test_another_agent_gets_not_found_but_the_owner_sees_it(git_repo, manager):
    started = await claude_code.HANDLERS["claude_code_start"](
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}},
        _ctx(),
    )
    job_id = started["job_id"]
    await manager.wait(job_id, "test-tenant", timeout_s=20)

    intruder = _ctx(agent_id="crm-hygiene")
    for name in ("claude_code_status", "claude_code_wait", "claude_code_cancel"):
        out = await claude_code.HANDLERS[name]({"job_id": job_id, "timeout_s": 1}, intruder)
        assert "not found" in out.get("error", ""), (name, out)
    out = await claude_code.HANDLERS["claude_code_followup"](
        {"job_id": job_id, "message": "x"}, intruder
    )
    assert "not found" in out.get("error", "")

    owner = _ctx(agent_id="crm-hygiene", user_role="owner")
    out = await claude_code.HANDLERS["claude_code_status"]({"job_id": job_id}, owner)
    assert out["job_id"] == job_id


async def test_start_without_repo_roots_names_the_setting(git_repo, manager, monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CODING_REPO_ROOTS")
    reset_settings()
    out = await claude_code.HANDLERS["claude_code_start"](
        {"task": "t", "repo_path": str(git_repo), "acceptance": {"verify_command": VERIFY}}, _ctx()
    )
    assert "ROBOTHOR_CODING_REPO_ROOTS" in out["error"]
