"""doctor: is there a container runtime, and where would each agent's exec run?"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from robothor.doctor.checks import sandbox as sandbox_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.tests.conftest import make_ctx
from robothor.engine.sandbox import RuntimeStatus


def _run(check_id: str):
    check = next(c for c in sandbox_checks.CHECKS if c.id == check_id)
    return asyncio.run(check.run(make_ctx()))


@pytest.fixture
def manifest_dir(monkeypatch, tmp_path) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(sandbox_checks, "_manifest_dir", lambda _ctx: directory)
    return directory


def _write(directory: Path, agent_id: str, body: str) -> None:
    (directory / f"{agent_id}.yaml").write_text(f"id: {agent_id}\nname: {agent_id}\n{body}")


def _runtime(monkeypatch, status: RuntimeStatus) -> None:
    monkeypatch.setattr("robothor.engine.sandbox.runtime_status", lambda **_k: status)


def test_registered() -> None:
    ids = {c.id for c in builtin_checks()}
    assert {"sandbox.runtime", "sandbox.policy"} <= ids


def test_runtime_present_passes_naming_the_binary(monkeypatch) -> None:
    _runtime(monkeypatch, RuntimeStatus(binary="podman", available=True, reason=""))
    result = _run("sandbox.runtime")
    assert result.status == "pass"
    assert "podman" in result.detail
    assert "available" in result.detail


def test_runtime_absent_is_reported_not_failed(monkeypatch) -> None:
    _runtime(
        monkeypatch,
        RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker"),
    )
    result = _run("sandbox.runtime")
    assert result.status == "pass"
    assert "not available" in result.detail
    assert "neither podman nor docker" in result.detail


def test_policy_lists_owner_and_member_per_exec_holding_agent(monkeypatch, manifest_dir) -> None:
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    _runtime(
        monkeypatch,
        RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker"),
    )
    _write(manifest_dir, "alice", "sandbox: docker\ntools_allowed: [exec]\n")
    _write(manifest_dir, "bob", "sandbox: host\ntools_allowed: [exec]\n")
    _write(manifest_dir, "carol", "tools_allowed: [read_file]\n")

    result = _run("sandbox.policy")

    assert result.status == "pass"
    lines = result.detail.splitlines()
    alice = next(line for line in lines if line.startswith("alice"))
    bob = next(line for line in lines if line.startswith("bob"))
    assert "owner host (degraded)" in alice and "member refuse" in alice
    assert "owner host" in bob and "member host" in bob
    assert not any(line.startswith("carol") for line in lines), "carol holds no exec"


def test_policy_with_the_flag_off_says_the_legacy_outcome(monkeypatch, manifest_dir) -> None:
    monkeypatch.delenv("ROBOTHOR_SANDBOX_PLAYGROUND", raising=False)
    _runtime(
        monkeypatch,
        RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker"),
    )
    _write(manifest_dir, "alice", "sandbox: docker\ntools_allowed: [exec]\n")

    result = _run("sandbox.policy")

    assert "playground off" in result.detail
    assert "alice" in result.detail and "run fails" in result.detail
