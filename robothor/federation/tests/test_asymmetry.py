"""The executable statement of the trust model.

The operator's requirement, verbatim: "Robothor would be like a master
controller at the highest level. I can build an organization of other units
underneath it that he can have some access to and control over, while also them
not having control over him."

Before 2026-08-27 the code did the opposite. `_OP_REQUIRED_CAPABILITY` mapped
BOTH `list_runs` and `trigger` to one capability, `agent_runs`, and that
capability was in `CHILD_DEFAULT_EXPORTS` -- so a child could execute arbitrary
agents on its parent by default. `_trigger` then called `runner.execute()` with
no user_role, which for a system trigger falls back to the allow-all `service`
role seeded by migration 107.

This file fails loudly if anyone widens a default again.
"""

from __future__ import annotations

import pytest

from robothor.federation.models import (
    CAP_READ_HEALTH,
    CAP_READ_RUNS,
    CAP_REPORT_UP,
    CAP_TRIGGER_AGENT,
    CHILD_DEFAULT_EXPORTS,
    PARENT_DEFAULT_EXPORTS,
    PEER_DEFAULT_EXPORTS,
    WRITE_CAPABILITIES,
    Relationship,
    default_exports_for,
)


class TestDefaultsEncodeTheHierarchy:
    def test_a_child_peer_may_only_report_upward(self):
        """A child gets ONE capability: push telemetry to us. Everything else --
        reading our runs, our health, our memory, and above all executing on us
        -- requires an explicit grant."""
        assert default_exports_for(Relationship.CHILD) == [CAP_REPORT_UP]

    def test_a_parent_peer_gets_read_only(self):
        """A parent that gets compromised should have a blast radius the child's
        operator chose deliberately."""
        assert set(default_exports_for(Relationship.PARENT)) == {CAP_READ_HEALTH, CAP_READ_RUNS}
        assert not (set(PARENT_DEFAULT_EXPORTS) & WRITE_CAPABILITIES)

    def test_a_symmetric_peer_negotiates_everything(self):
        assert default_exports_for(Relationship.PEER) == []
        assert PEER_DEFAULT_EXPORTS == []

    @pytest.mark.parametrize(
        "template,name",
        [
            (CHILD_DEFAULT_EXPORTS, "CHILD"),
            (PARENT_DEFAULT_EXPORTS, "PARENT"),
            (PEER_DEFAULT_EXPORTS, "PEER"),
        ],
    )
    def test_no_write_capability_is_ever_a_default(self, template, name):
        leaked = WRITE_CAPABILITIES & set(template)
        assert not leaked, (
            f"{name}_DEFAULT_EXPORTS grants {sorted(leaked)} without anyone asking. "
            f"trigger_agent and push_config change the receiving instance and must "
            f"require an explicit, audited grant."
        )

    def test_the_defaults_are_not_symmetric(self):
        """The core property. If these two ever match, the hierarchy is gone."""
        assert set(CHILD_DEFAULT_EXPORTS) != set(PARENT_DEFAULT_EXPORTS)


class TestReadingIsNotExecuting:
    def test_list_and_trigger_need_different_capabilities(self):
        from robothor.engine.tools.handlers.federation import _OP_REQUIRED_CAPABILITY

        assert _OP_REQUIRED_CAPABILITY["list_runs"] != _OP_REQUIRED_CAPABILITY["trigger"], (
            "reading what an instance did and making it do something new are not "
            "the same authority; collapsing them is what handed every child the "
            "right to execute on its parent"
        )

    def test_trigger_requires_the_write_capability(self):
        from robothor.engine.tools.handlers.federation import _OP_REQUIRED_CAPABILITY

        assert _OP_REQUIRED_CAPABILITY["trigger"] == CAP_TRIGGER_AGENT
        assert CAP_TRIGGER_AGENT in WRITE_CAPABILITIES


class TestTheCapabilityGateRefuses:
    """Gate 1, in both directions -- and they are different gates.

    `_authorize_op` reads `connection.imports`: what the PEER granted US, so it
    governs what we may ask of them (OUTBOUND). The responder reads
    `connection.exports`: what WE granted THEM, so it governs what they may ask
    of us (INBOUND). Conflating the two is how a direction-blind capability
    model gets written in the first place.
    """

    @staticmethod
    def _conn(*, imports=(), exports=()):
        class _C:
            pass

        c = _C()
        c.id = "c1"
        c.imports = list(imports)
        c.exports = list(exports)
        c.peer_name = "probe"
        c.state = "active"  # gate 0: only an activated link carries traffic
        return c

    # ── OUTBOUND: what a peer granted us ──────────────────────────────
    def test_we_cannot_trigger_a_peer_that_only_granted_us_reads(self):
        from robothor.engine.tools.handlers.federation import _authorize_op

        conn = self._conn(imports=PARENT_DEFAULT_EXPORTS)  # they treat us as their parent
        assert _authorize_op(conn, "list_runs") is None
        assert _authorize_op(conn, "trigger"), (
            "we were authorized to trigger on a peer that granted only reads"
        )

    def test_we_can_do_nothing_to_a_peer_that_granted_child_defaults(self):
        from robothor.engine.tools.handlers.federation import _authorize_op

        conn = self._conn(imports=CHILD_DEFAULT_EXPORTS)  # they treat us as their child
        assert _authorize_op(conn, "list_runs")
        assert _authorize_op(conn, "trigger")

    # ── INBOUND: what we granted a peer ───────────────────────────────
    @pytest.mark.asyncio
    async def test_a_default_child_asking_us_to_trigger_is_refused(self):
        import json

        from robothor.engine.federation_responder import make_command_handler

        handler = make_command_handler(self._conn(exports=CHILD_DEFAULT_EXPORTS), runner=None)
        out = json.loads(await handler(json.dumps({"op": "trigger", "agent_id": "main"}).encode()))
        assert "error" in out, "a default child was allowed to trigger on us"
        assert "not exported" in out["error"] or "denied" in out["error"]

    @pytest.mark.asyncio
    async def test_a_default_child_asking_for_our_runs_is_refused(self):
        import json

        from robothor.engine.federation_responder import make_command_handler

        handler = make_command_handler(self._conn(exports=CHILD_DEFAULT_EXPORTS), runner=None)
        out = json.loads(await handler(json.dumps({"op": "list_runs"}).encode()))
        assert "error" in out

    @pytest.mark.asyncio
    async def test_an_unknown_op_is_refused_before_anything_else(self):
        import json

        from robothor.engine.federation_responder import make_command_handler

        handler = make_command_handler(self._conn(exports=["*"]), runner=None)
        out = json.loads(await handler(json.dumps({"op": "rm_rf"}).encode()))
        assert "unknown op" in out.get("error", "")


@pytest.mark.integration
class TestTheAuthorizationGateRefuses:
    """Gate 2: what this principal may do locally. Needs the migration-112 seeds."""

    @pytest.mark.parametrize("tool", ["list_agent_runs", "exec", "get_stats", "search_memory"])
    def test_a_child_principal_is_denied_everything(self, tool):
        from robothor.constants import DEFAULT_TENANT
        from robothor.engine.permissions import check_tool_permission

        try:
            denial = check_tool_permission("federation_child", DEFAULT_TENANT, tool)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no database: {exc}")
        assert denial, (
            f"federation_child was permitted {tool!r}. Deny-all is what makes "
            f"'a child has no control over its parent' the default rather than a "
            f"checkbox someone has to remember."
        )

    @pytest.mark.parametrize(
        "tool,should_allow",
        [
            ("list_agent_runs", True),
            ("get_stats", True),
            ("search_memory", True),
            ("exec", False),
            ("write_file", False),
        ],
    )
    def test_a_parent_principal_reads_but_does_not_write(self, tool, should_allow):
        from robothor.constants import DEFAULT_TENANT
        from robothor.engine.permissions import check_tool_permission

        try:
            denial = check_tool_permission("federation_parent", DEFAULT_TENANT, tool)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no database: {exc}")
        assert (denial is None) is should_allow, f"{tool}: denial={denial!r}"
