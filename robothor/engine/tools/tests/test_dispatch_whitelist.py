"""Tests for the per-task tool whitelist ContextVar (Rip 1 dispatch)."""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.tools.dispatch import (
    _execute_tool,
    clear_tool_whitelist,
    get_tool_whitelist,
    set_tool_whitelist,
)


@pytest.fixture(autouse=True)
def seeded_service_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise whitelist ordering without depending on the RBAC database."""

    monkeypatch.setattr(
        "robothor.engine.permissions.check_tool_permission",
        lambda role, tenant, tool: (
            None if role == "service" else "Missing execution role — access denied"
        ),
    )


class TestWhitelistContextVar:
    def test_default_is_none(self) -> None:
        assert get_tool_whitelist() is None

    def test_set_and_clear(self) -> None:
        token = set_tool_whitelist(frozenset({"memory_search"}))
        try:
            assert get_tool_whitelist() == frozenset({"memory_search"})
        finally:
            clear_tool_whitelist(token)
        assert get_tool_whitelist() is None

    def test_asyncio_task_isolation(self) -> None:
        """ContextVar is per-task — sibling tasks must not leak."""
        captured: dict[str, frozenset[str] | None] = {}

        async def run_in_isolated_task(label: str, allowed: frozenset[str] | None) -> None:
            if allowed is not None:
                token = set_tool_whitelist(allowed)
                try:
                    await asyncio.sleep(0)
                    captured[label] = get_tool_whitelist()
                finally:
                    clear_tool_whitelist(token)
            else:
                await asyncio.sleep(0)
                captured[label] = get_tool_whitelist()

        async def main() -> None:
            t1 = asyncio.create_task(
                run_in_isolated_task("with_whitelist", frozenset({"memory_search"}))
            )
            t2 = asyncio.create_task(run_in_isolated_task("no_whitelist", None))
            await t1
            await t2

        asyncio.run(main())

        assert captured["with_whitelist"] == frozenset({"memory_search"})
        assert captured["no_whitelist"] is None


class TestExecuteToolEnforcesWhitelist:
    @pytest.mark.asyncio
    async def test_unknown_tool_through_whitelist(self) -> None:
        """A tool outside the whitelist is bounced even if it would
        otherwise have hit 'unknown tool' — the whitelist gate fires
        first."""
        token = set_tool_whitelist(frozenset({"memory_search"}))
        try:
            result = await _execute_tool("send_telegram", {}, user_role="service")
        finally:
            clear_tool_whitelist(token)

        assert result.get("error", "").startswith("Tool 'send_telegram' denied")
        assert result.get("denied_by_whitelist") is True

    @pytest.mark.asyncio
    async def test_whitelisted_tool_passes_gate(self) -> None:
        """A whitelisted-but-unknown tool gets past the whitelist gate
        and falls through to the standard 'unknown tool' error from
        the handler lookup — proving the gate let it through."""
        token = set_tool_whitelist(frozenset({"some_made_up_tool"}))
        try:
            result = await _execute_tool("some_made_up_tool", {}, user_role="service")
        finally:
            clear_tool_whitelist(token)

        # NOT denied by whitelist; whatever happens next is the
        # handler-lookup error (Unknown tool, etc), which is the
        # signal the gate let us through.
        assert "denied_by_whitelist" not in result

    @pytest.mark.asyncio
    async def test_no_whitelist_no_enforcement(self) -> None:
        """When no whitelist is installed, the gate is invisible —
        execution falls through to whatever the normal path would do."""
        assert get_tool_whitelist() is None
        result = await _execute_tool("some_made_up_tool", {}, user_role="service")
        # Standard 'Unknown tool' path; no denied_by_whitelist flag.
        assert "denied_by_whitelist" not in result
