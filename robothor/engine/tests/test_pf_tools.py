"""Tests for Princess Freya (PF) vessel tool handlers."""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import PF_TOOLS, READONLY_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.pf import HANDLERS


class TestPFToolConstants:
    def test_pf_tools_frozenset(self):
        assert "pf_system_status" in PF_TOOLS

    def test_pf_tools_in_readonly(self):
        assert "pf_system_status" in READONLY_TOOLS

    def test_handlers_registered(self):
        assert "pf_system_status" in HANDLERS


class TestPFSystemStatus:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self):
        ctx = ToolContext(agent_id="pf-watchdog", tenant_id="robothor-pf")
        result = await HANDLERS["pf_system_status"]({}, ctx)

        assert "error" not in result
        assert "disk" in result
        assert "memory" in result
        assert "connectivity" in result
        assert "uptime" in result

    @pytest.mark.asyncio
    async def test_disk_has_expected_fields(self):
        ctx = ToolContext(agent_id="pf-helm", tenant_id="robothor-pf")
        result = await HANDLERS["pf_system_status"]({}, ctx)

        disk = result["disk"]
        assert "total_gb" in disk
        assert "free_gb" in disk
        assert "used_pct" in disk
        assert isinstance(disk["used_pct"], float)
        assert 0 <= disk["used_pct"] <= 100

    @pytest.mark.asyncio
    async def test_memory_has_expected_fields(self):
        ctx = ToolContext(agent_id="pf-helm", tenant_id="robothor-pf")
        result = await HANDLERS["pf_system_status"]({}, ctx)

        mem = result["memory"]
        assert mem is not None
        assert "total_mb" in mem
        assert "available_mb" in mem
        assert "used_pct" in mem

    @pytest.mark.asyncio
    async def test_connectivity_checks(self):
        ctx = ToolContext(agent_id="pf-helm", tenant_id="robothor-pf")
        result = await HANDLERS["pf_system_status"]({}, ctx)

        conn = result["connectivity"]
        assert "tailscale" in conn
        assert "internet" in conn
        assert "parent" in conn
        # All values should be booleans
        for key in ("tailscale", "internet", "parent"):
            assert isinstance(conn[key], bool)
