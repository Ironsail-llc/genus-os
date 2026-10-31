"""Install-time fallbacks leave a trace.

Each of these used to be `except Exception: pass`. The fallback stays (a
missing optional scorer or registry must not fail an install), but the reason
the check was skipped is now in the log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _logged(caplog: pytest.LogCaptureFixture, logger: str, needle: str) -> bool:
    return any(r.name == logger and needle in r.getMessage() for r in caplog.records)


def test_check_tools_registered_logs_an_adapter_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.templates.manifest_checks import check_tools_registered

    def _boom(agent_id: str) -> Any:
        raise RuntimeError("adapters.yaml unreadable")

    monkeypatch.setattr("robothor.engine.adapters.get_adapters_for_agent", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.templates.manifest_checks"):
        result = check_tools_registered(
            {"id": "example-agent", "tools_allowed": ["no_such_tool"]}, {"read_file"}
        )
    assert result.status == "FAIL"
    assert _logged(caplog, "robothor.templates.manifest_checks", "adapters.yaml unreadable")


def test_validate_post_install_logs_a_registry_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.templates import validators

    (tmp_path / "docs" / "agents").mkdir(parents=True)
    manifest_path = tmp_path / "docs" / "agents" / "example-agent.yaml"
    manifest_path.write_text(yaml.safe_dump({"id": "example-agent"}))

    class _Broken:
        def __init__(self) -> None:
            raise RuntimeError("tool schemas failed to load")

    seen: dict[str, Any] = {}

    def _validate(manifest: Any, all_manifests: Any, registered: set[str], repo_root: Any) -> list:
        seen["registered"] = registered
        return []

    monkeypatch.setattr("robothor.engine.tools.ToolRegistry", _Broken)
    monkeypatch.setattr("robothor.templates.manifest_checks.validate_agent", _validate)
    with caplog.at_level(logging.DEBUG, logger="robothor.templates.validators"):
        messages = validators.validate_post_install(manifest_path, repo_root=tmp_path)
    assert messages == []
    assert seen["registered"] == set()
    assert _logged(caplog, "robothor.templates.validators", "tool schemas failed to load")


def test_import_agent_logs_a_hub_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_repo: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.templates.installer import import_agent

    (tmp_repo / "docs" / "agents" / "my-agent.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "my-agent",
                "name": "My Agent",
                "description": "A test agent",
                "version": "2026-03-01",
                "department": "custom",
                "instruction_file": "brain/MY_AGENT.md",
            }
        )
    )
    (tmp_repo / "brain" / "MY_AGENT.md").write_text("# My Agent\n")

    def _boom(path: Any) -> Any:
        raise RuntimeError("scorer crashed")

    monkeypatch.setattr("robothor.templates.description_optimizer.score_hub_readiness", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.templates.installer"):
        result = import_agent("my-agent", output_dir=str(tmp_path / "out"), repo_root=tmp_repo)
    assert result["agent_id"] == "my-agent"
    assert _logged(caplog, "robothor.templates.installer", "scorer crashed")
