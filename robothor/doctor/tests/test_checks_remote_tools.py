"""``capabilities.remote_tools``: can this tenant's agent reach the bridge's remote routes?

The remote routes are authorised by the ``org:remote`` scope, so an agent the
bridge's capabilities manifest does not list is let through. One that it DOES
list must carry both remote patterns in ``bridge_endpoints``, or every remote
call is refused. The check says so, names the agent, the file and the lines
to add, but only where it matters: remote tools on and the tenant has children.

Generic fixture agents only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from robothor.doctor.context import DoctorContext
from robothor.tenancy.remote_contract import ORG_REMOTE_ENDPOINTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

PARENT, CHILD = "acme-parent", "acme-child-a"


def _check():
    from robothor.doctor.checks.capabilities import CHECKS

    return {check.id: check for check in CHECKS}["capabilities.remote_tools"]


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from robothor.events import capabilities
    from robothor.settings import reset_settings

    workspace = tmp_path / "workspace"
    (workspace / "docs" / "agents").mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(workspace / "docs" / "agents"))
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", PARENT)
    monkeypatch.setenv("ROBOTHOR_CAPABILITIES_MANIFEST", str(workspace / "caps.json"))
    reset_settings()
    capabilities.reset()
    yield workspace
    capabilities.reset()
    reset_settings()


def _caps(workspace: Path, agents: dict) -> None:
    (workspace / "caps.json").write_text(json.dumps({"agents": agents}))


def _agent(workspace: Path, agent_id: str, manifest: dict) -> None:
    body = {"id": agent_id, "name": agent_id, **manifest}
    (workspace / "docs" / "agents" / f"{agent_id}.yaml").write_text(yaml.safe_dump(body))


async def _rows(children: bool = True) -> list:
    reach = (PARENT, CHILD) if children else (PARENT,)
    with (
        patch("robothor.flags.store.resolve", side_effect=lambda name: None),
        patch("robothor.engine.permissions.resolve_accessible_tenants", return_value=reach),
    ):
        out = await _check().run(DoctorContext(offline=True))
    return out if isinstance(out, list) else [out]


def test_registered_as_an_info_check_with_an_operator_docstring() -> None:
    check = _check()
    assert check.category == "capabilities"
    assert check.run.__doc__


@pytest.mark.asyncio
async def test_main_listed_without_the_patterns_fails_naming_what_to_add(instance) -> None:
    _caps(instance, {"main": {"tools": [], "bridge_endpoints": ["GET /api/people/*"]}})

    (row,) = await _rows()

    assert row.status == "fail"
    assert "main" in row.detail
    assert "caps.json" in row.detail
    for pattern in ORG_REMOTE_ENDPOINTS:
        assert pattern in row.detail
    assert "restart" in row.detail.lower()


@pytest.mark.asyncio
async def test_main_listed_with_both_patterns_passes(instance) -> None:
    _caps(instance, {"main": {"tools": [], "bridge_endpoints": list(ORG_REMOTE_ENDPOINTS)}})

    (row,) = await _rows()

    assert row.status == "pass"


@pytest.mark.asyncio
async def test_an_unlisted_main_passes_because_the_scope_authorises_it(instance) -> None:
    _caps(instance, {"crm-steward": {"tools": [], "bridge_endpoints": ["GET /health"]}})

    (row,) = await _rows()

    assert row.status == "pass"


@pytest.mark.asyncio
async def test_another_agent_holding_the_tools_is_checked_too(instance) -> None:
    _agent(instance, "ops-agent", {"tools_allowed": ["org_tool_read"]})
    _caps(instance, {"ops-agent": {"tools": [], "bridge_endpoints": ["GET /health"]}})

    rows = await _rows()

    assert [r.status for r in rows] == ["fail"]
    assert "ops-agent" in rows[0].detail


@pytest.mark.asyncio
async def test_skipped_without_children(instance) -> None:
    _caps(instance, {"main": {"tools": [], "bridge_endpoints": []}})

    (row,) = await _rows(children=False)

    assert row.status == "skip"
