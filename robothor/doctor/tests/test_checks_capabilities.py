"""``genus doctor --category capabilities [--agent ID]``: what each agent can do.

One row per ``<agent>:<capability>``. A capability the agent has passes; one it
lacks fails at ``info`` severity (never a verdict on the instance) and the
detail names the gate and the unlock, the same words the agent's own prompt
block uses, because both come from ``robothor.engine.capabilities.assess``.

Generic fixture agents only.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from robothor.doctor.context import DoctorContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path


def _check(check_id: str = "capabilities.agent"):
    from robothor.doctor.checks.capabilities import CHECKS

    return {check.id: check for check in CHECKS}[check_id]


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from robothor.settings import reset_settings

    workspace = tmp_path / "workspace"
    (workspace / "docs" / "agents").mkdir(parents=True)
    (workspace / "brain").mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(workspace / "docs" / "agents"))
    monkeypatch.delenv("ROBOTHOR_CODING_REPO_ROOTS", raising=False)
    reset_settings()
    yield workspace
    reset_settings()


def _write_agent(workspace: Path, agent_id: str, manifest: dict) -> None:
    manifest = {"id": agent_id, "name": agent_id, **manifest}
    (workspace / "docs" / "agents" / f"{agent_id}.yaml").write_text(yaml.safe_dump(manifest))


async def _rows(agent: str = "") -> list:
    with (
        patch("robothor.secrets.secret_source", return_value="missing"),
        patch(
            "robothor.engine.coding.runner.resolve_claude_binary",
            side_effect=__import__(
                "robothor.engine.coding.runner", fromlist=["ClaudeCodeError"]
            ).ClaudeCodeError("not found"),
        ),
    ):
        out = await _check().run(DoctorContext(offline=True, agent=agent))
    return out if isinstance(out, list) else [out]


def _by_sub(rows: list) -> dict:
    return {row.sub_id: row for row in rows}


def test_the_check_is_registered_after_tools() -> None:
    from robothor.doctor.registry import CHECK_MODULES

    assert "capabilities" in CHECK_MODULES
    assert CHECK_MODULES.index("capabilities") == CHECK_MODULES.index("tools") + 1
    check = _check()
    assert check.category == "capabilities"
    assert check.severity == "info"


@pytest.mark.asyncio
async def test_one_row_per_agent_and_capability(instance: Path) -> None:
    _write_agent(instance, "example-agent", {"tools_allowed": ["exec", "write_file"]})
    _write_agent(instance, "other-agent", {"tools_allowed": ["read_file"]})
    rows = _by_sub(await _rows())
    assert rows["example-agent:exec"].status == "pass"
    assert rows["example-agent:write_files"].status == "pass"
    assert rows["other-agent:exec"].status == "fail"
    assert "docs/agents/other-agent.yaml" in rows["other-agent:exec"].detail


@pytest.mark.asyncio
async def test_coding_jobs_without_repo_roots_names_the_runtime_gate(instance: Path) -> None:
    _write_agent(instance, "main", {"tools_opt_in": ["claude_code_start"]})
    rows = _by_sub(await _rows("main"))
    coding = rows["main:coding_jobs"]
    assert coding.status == "fail"
    assert "runtime" in coding.detail
    assert "ROBOTHOR_CODING_REPO_ROOTS" in coding.detail


@pytest.mark.asyncio
async def test_a_missing_github_token_gates_git_pr(instance: Path) -> None:
    _write_agent(instance, "main", {"tools_allowed": ["git_commit", "create_pull_request"]})
    rows = _by_sub(await _rows("main"))
    assert rows["main:git_pr"].status == "fail"
    assert "GITHUB_TOKEN" in rows["main:git_pr"].detail


@pytest.mark.asyncio
async def test_agent_filter_reports_only_that_agent(instance: Path) -> None:
    _write_agent(instance, "main", {"tools_allowed": ["exec"]})
    _write_agent(instance, "other-agent", {"tools_allowed": ["exec"]})
    rows = await _rows("main")
    assert rows
    assert {row.sub_id.split(":")[0] for row in rows} == {"main"}


@pytest.mark.asyncio
async def test_an_unknown_agent_is_one_fail_row_naming_it(instance: Path) -> None:
    _write_agent(instance, "main", {"tools_allowed": ["exec"]})
    rows = await _rows("nobody-here")
    assert len(rows) == 1
    assert rows[0].status == "fail"
    assert "nobody-here" in rows[0].detail


def test_the_cli_accepts_agent_and_hands_it_to_the_context() -> None:
    from robothor.cli import _build_parser, doctor_cmd

    args = _build_parser().parse_args(
        ["doctor", "--category", "capabilities", "--agent", "main", "--json"]
    )
    assert args.agent == "main"
    seen: dict = {}

    def _fake_run(ctx, **kw):
        seen["ctx"] = ctx
        seen.update(kw)
        return argparse.Namespace()

    with (
        patch.object(doctor_cmd, "run_sync", side_effect=_fake_run),
        patch.object(doctor_cmd, "_load_instance_env"),
    ):
        doctor_cmd.run_doctor(args)
    assert seen["ctx"].agent == "main"
    assert seen["category"] == "capabilities"


@pytest.mark.asyncio
async def test_vault_set_without_a_tier_is_gated_on_the_tier(instance: Path, monkeypatch) -> None:
    from robothor.engine.tools.handlers import vault

    monkeypatch.setenv("ROBOTHOR_CREDENTIAL_INTAKE", "enforce")
    monkeypatch.delattr(vault, "_intake_gate", raising=False)
    _write_agent(instance, "main", {"tools_allowed": ["vault_set"]})
    row = _by_sub(await _rows("main"))["main:vault_set"]
    assert row.status == "fail"
    assert "credentials: operator" in row.detail
    assert "ROBOTHOR_CREDENTIAL_INTAKE" not in row.detail


@pytest.mark.asyncio
async def test_vault_set_passes_for_the_chat_agent_where_the_build_has_intake(
    instance: Path, monkeypatch
) -> None:
    from robothor.engine.tools.handlers import vault

    monkeypatch.setattr(vault, "_intake_gate", lambda ctx: None, raising=False)
    _write_agent(instance, "main", {"tools_allowed": ["vault_set"]})
    row = _by_sub(await _rows("main"))["main:vault_set"]
    assert row.status == "pass"
    assert "owner or admin" in row.detail


@pytest.mark.asyncio
async def test_vault_set_with_the_operator_tier_passes(instance: Path) -> None:
    _write_agent(
        instance, "main", {"tools_allowed": ["vault_set"], "v2": {"credentials": "operator"}}
    )
    assert _by_sub(await _rows("main"))["main:vault_set"].status == "pass"


@pytest.mark.asyncio
async def test_the_coding_workspace_counts_only_where_the_build_reads_it(
    instance: Path, monkeypatch
) -> None:
    from robothor.engine import capabilities

    monkeypatch.setenv("ROBOTHOR_CODING_WORKSPACE", str(instance / "coding"))
    _write_agent(instance, "main", {"tools_opt_in": ["claude_code_start"]})

    async def coding_row():
        with patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="claude"):
            rows = _by_sub(await _check().run(DoctorContext(offline=True, agent="main")))
        return rows["main:coding_jobs"]

    monkeypatch.setattr(capabilities, "coding_workspace", lambda settings: None)
    row = await coding_row()
    assert row.status == "fail"
    assert "ROBOTHOR_CODING_WORKSPACE" not in row.detail
    monkeypatch.setattr(capabilities, "coding_workspace", lambda settings: str(instance / "coding"))
    assert (await coding_row()).status == "pass"
