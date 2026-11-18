"""The ORGANISATION block: which tenant the agent serves, and where it sits.

An owner of a parent tenant asked the operator-facing agent about the
organisation, and it knew neither the organisation's name nor that it had
child tenants; it read a misspelling of the organisation's own name as a
company to look up. The engine context now names this tenant, its parent, the
child tenants this run may reach, and what the run may do in them, on every
interactive turn and in the scheduled-run preamble.

The DAL is mocked: the block reads ``crm_tenants`` through
``robothor.crm.dal.get_tenant`` / ``list_tenants``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig

PARENT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

_TENANTS: dict[str, dict[str, Any]] = {
    PARENT: {"id": PARENT, "displayName": "Acme Holdings", "parentTenantId": None},
    CHILD_A: {"id": CHILD_A, "displayName": "Acme Child A", "parentTenantId": PARENT},
    CHILD_B: {"id": CHILD_B, "displayName": "Acme Child B", "parentTenantId": PARENT},
}


def _get_tenant(tenant_id: str) -> dict[str, Any] | None:
    return _TENANTS.get(tenant_id)


def _list_tenants(parent_id: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    return [t for t in _TENANTS.values() if t["parentTenantId"] == parent_id]


@pytest.fixture
def tenant_tree():
    with (
        patch("robothor.crm.dal.get_tenant", side_effect=_get_tenant),
        patch("robothor.crm.dal.list_tenants", side_effect=_list_tenants),
    ):
        yield


def _block(tenant_id: str, accessible: tuple[str, ...] | None) -> str:
    from robothor.engine.org_context import organisation_block

    return organisation_block(tenant_id, accessible)


class TestOrganisationBlock:
    @pytest.mark.usefixtures("tenant_tree")
    def test_owner_of_parent_sees_tenant_and_both_children(self) -> None:
        text = _block(PARENT, (PARENT, CHILD_A, CHILD_B))

        assert text.startswith("--- ORGANISATION ---")
        assert "Acme Holdings" in text and PARENT in text
        assert "Acme Child A" in text and CHILD_A in text
        assert "Acme Child B" in text and CHILD_B in text
        assert "top-level" in text
        # One plain line on what the run may do there.
        assert "may read and act in this tenant and in the child tenants" in text

    @pytest.mark.usefixtures("tenant_tree")
    def test_member_of_parent_sees_only_own_tenant(self) -> None:
        text = _block(PARENT, (PARENT,))

        assert "Acme Holdings" in text
        assert CHILD_A not in text and "Acme Child A" not in text
        assert CHILD_B not in text and "Acme Child B" not in text
        assert "only in this tenant" in text

    @pytest.mark.usefixtures("tenant_tree")
    def test_child_tenant_names_its_parent_and_has_no_children(self) -> None:
        text = _block(CHILD_A, (CHILD_A,))

        assert "Acme Child A" in text
        assert "Parent tenant: Acme Holdings" in text and PARENT in text
        assert "Child tenants: none" in text

    @pytest.mark.usefixtures("tenant_tree")
    def test_no_accessible_ids_means_own_tenant_only(self) -> None:
        text = _block(PARENT, None)

        assert CHILD_A not in text
        assert "only in this tenant" in text

    @pytest.mark.usefixtures("tenant_tree")
    def test_names_are_tenants_not_crm_companies(self) -> None:
        text = _block(PARENT, (PARENT, CHILD_A, CHILD_B))

        assert "not a CRM company" in text

    def test_missing_display_name_falls_back_to_id(self) -> None:
        with (
            patch(
                "robothor.crm.dal.get_tenant",
                return_value={"id": PARENT, "displayName": "", "parentTenantId": None},
            ),
            patch("robothor.crm.dal.list_tenants", return_value=[]),
        ):
            text = _block(PARENT, (PARENT,))
        assert f"You serve: {PARENT}" in text

    def test_failed_lookup_degrades_to_nothing_with_a_warning(self, caplog) -> None:
        with (
            patch("robothor.crm.dal.get_tenant", side_effect=RuntimeError("db down")),
            caplog.at_level(logging.WARNING, logger="robothor.engine.org_context"),
        ):
            text = _block(PARENT, (PARENT, CHILD_A))

        assert text == ""
        assert any("organisation" in r.getMessage().lower() for r in caplog.records)

    def test_unknown_tenant_row_degrades_to_nothing(self) -> None:
        with (
            patch("robothor.crm.dal.get_tenant", return_value=None),
            patch("robothor.crm.dal.list_tenants", return_value=[]),
        ):
            assert _block("acme-missing", ("acme-missing",)) == ""


@pytest.mark.usefixtures("tenant_tree")
class TestOrganisationInPreambles:
    def test_first_interactive_turn_carries_the_block(self) -> None:
        from robothor.engine.warmup import _CONTEXT_HOOKS, build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch("robothor.memory.blocks.read_blocks", return_value={}):
                text = build_interactive_preamble(
                    "main",
                    user_message="",
                    include_blocks=False,
                    tenant_id=PARENT,
                    sender_name="Alice",
                    accessible_tenant_ids=(PARENT, CHILD_A, CHILD_B),
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "--- ORGANISATION ---" in text
        assert "Acme Child B" in text
        # Next to the CURRENT USER block, ahead of everything else.
        assert text.index("--- CURRENT USER ---") < text.index("--- ORGANISATION ---")

    def test_cron_preamble_carries_the_block(self, tmp_path) -> None:
        from robothor.engine.warmup import build_warmth_preamble

        config = AgentConfig(id="worker", name="Worker", warmup_memory_blocks=["x"])
        with patch("robothor.memory.blocks.read_blocks", return_value={}):
            text, timings = build_warmth_preamble(
                config, tmp_path, PARENT, accessible_tenant_ids=(PARENT,)
            )

        assert "--- ORGANISATION ---" in text
        assert "Acme Holdings" in text
        assert "organisation" in timings


class TestFleetRoster:
    _ROWS = [
        {"agent_id": "email-classifier", "enabled": True, "cron_expr": "*/15 * * * *",
         "last_status": "completed"},
        {"agent_id": "crm-steward", "enabled": False, "cron_expr": "0 3 * * *",
         "last_status": None},
    ]  # fmt: skip

    def test_main_gets_the_roster_with_schedule_state(self) -> None:
        from robothor.engine.warmup import _CONTEXT_HOOKS, build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch("robothor.engine.tracking.list_schedules", return_value=self._ROWS),
                patch("robothor.crm.dal.list_tasks", return_value=[]),
            ):
                text = build_interactive_preamble(
                    "main", include_blocks=False, tenant_id=PARENT, sender_name="Alice"
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "--- FLEET ROSTER ---" in text
        roster = text.split("--- FLEET ROSTER ---", 1)[1]
        assert "email-classifier" in roster and "*/15 * * * *" in roster
        assert "enabled" in roster and "completed" in roster
        assert "crm-steward" in roster and "disabled" in roster

    def test_other_agents_do_not_get_the_roster(self) -> None:
        from robothor.engine.warmup import _CONTEXT_HOOKS, build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch("robothor.engine.tracking.list_schedules", return_value=self._ROWS):
                text = build_interactive_preamble(
                    "worker", include_blocks=False, tenant_id=PARENT, sender_name="Alice"
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "FLEET ROSTER" not in text

    def test_roster_failure_omits_the_section(self) -> None:
        from robothor.engine.warmup import _fleet_roster_section

        with patch("robothor.engine.tracking.list_schedules", side_effect=RuntimeError("x")):
            assert _fleet_roster_section(PARENT) == ""
