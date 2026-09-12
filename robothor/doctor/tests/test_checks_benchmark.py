"""``benchmark.isolation`` -- is a graded run allowed anywhere near production?

The check exists because of incident 2026-09-12: this instance ran a nightly
benchmark for eleven days with the sandbox ``off``, which the runbook described
as "sub-runs stay read-only" and which was not true. Nothing told the operator
that a benchmark schedule and a disabled sandbox were a combination, so nobody
looked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 - used at runtime by the fixtures
from typing import Any

import pytest

from robothor.doctor.checks import config as config_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx

_CHECK_ID = "benchmark.isolation"


def _run(ctx: Any) -> Any:
    check = next(check for check in config_checks.CHECKS if check.id == _CHECK_ID)
    return asyncio.run(check.run(ctx))


@pytest.fixture
def workspace(monkeypatch: Any, tmp_path: Path) -> Path:
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_settings()
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    return tmp_path


def _manifest(workspace: Path, agent_id: str, body: str) -> None:
    (workspace / "docs" / "agents" / f"{agent_id}.yaml").write_text(
        f"id: {agent_id}\nname: {agent_id}\n{body}"
    )


def _sandbox_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("ROBOTHOR_BENCHMARK_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("ROBOTHOR_BENCHMARK_SANDBOX_MODE", raising=False)


def _sandbox_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_SANDBOX_MODE", "observe")


def test_it_fails_when_a_benchmark_is_scheduled_and_the_sandbox_is_off(
    monkeypatch: Any, workspace: Path
) -> None:
    _sandbox_off(monkeypatch)
    _manifest(workspace, "nightly-grader", "tools_allowed: [benchmark_run_fleet, search_memory]\n")
    result = _run(make_ctx(db_factory=fake_db([("nightly-grader",)])))

    assert result.status == "fail"
    assert "nightly-grader" in result.detail
    assert "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED" in result.detail
    assert "ROBOTHOR_BENCHMARK_SANDBOX_MODE" in result.detail


def test_a_manifest_flagged_is_benchmark_counts_too(monkeypatch: Any, workspace: Path) -> None:
    _sandbox_off(monkeypatch)
    _manifest(workspace, "grader", "is_benchmark: true\n")
    result = _run(make_ctx(db_factory=fake_db([("grader",)])))
    assert result.status == "fail"
    assert "grader" in result.detail


def test_it_passes_when_the_sandbox_is_on(monkeypatch: Any, workspace: Path) -> None:
    _sandbox_on(monkeypatch)
    _manifest(workspace, "nightly-grader", "tools_allowed: [benchmark_run_fleet]\n")
    result = _run(make_ctx(db_factory=fake_db([("nightly-grader",)])))
    assert result.status == "pass"
    assert "observe" in result.detail


def test_it_passes_when_no_benchmark_is_scheduled(monkeypatch: Any, workspace: Path) -> None:
    _sandbox_off(monkeypatch)
    _manifest(workspace, "mailer", "tools_allowed: [gws_gmail_send]\n")
    result = _run(make_ctx(db_factory=fake_db([("mailer",)])))
    assert result.status == "pass"


def test_a_scheduled_agent_with_no_manifest_is_not_a_benchmark(
    monkeypatch: Any, workspace: Path
) -> None:
    """A schedule can outlive its manifest; that is ``manifests.*``'s finding,
    not a reason to claim this instance benchmarks against production."""
    _sandbox_off(monkeypatch)
    result = _run(make_ctx(db_factory=fake_db([("vanished",)])))
    assert result.status == "pass"


def test_an_unreachable_database_skips_rather_than_passing(
    monkeypatch: Any, workspace: Path
) -> None:
    _sandbox_off(monkeypatch)
    result = _run(make_ctx(db_factory=fake_db(error=RuntimeError("no database"))))
    assert result.status == "skip"
    assert "no database" in result.detail or "RuntimeError" in result.detail


def test_the_check_is_registered_and_recommended() -> None:
    from robothor.doctor.registry import builtin_checks

    check = next(check for check in builtin_checks() if check.id == _CHECK_ID)
    assert check.category == "config"
    assert check.severity == "recommended"
