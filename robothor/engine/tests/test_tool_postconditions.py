"""Tool-level post-condition verification — grade the environment, not the transcript.

The live failure this exists for: a run reported "✅ Payment confirmed" whose
entire tool trace was one ``write_file`` to ``/tmp``. Every guardrail on the
box was structurally blind to it. These tests pin the opposite discipline —
after a side-effectful tool reports success, an independent read-back of the
environment decides whether it actually happened.

Pure unit tests: no DB, no Gmail, no LLM. The one environment read each
checker makes is patched at the module boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tools import verification
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.verification import (
    POST_CONDITION_CHECKS,
    VerificationOutcome,
    reset_verification_budget,
    verify_tool_result,
)


@pytest.fixture(autouse=True)
def _clean_budget() -> None:
    """Per-run check counters must not leak between tests."""
    reset_verification_budget()


@pytest.fixture
def observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "observe")


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "enforce")


@pytest.fixture
def evidence() -> Any:
    """Capture ledger writes without touching the database."""
    with patch.object(verification, "_insert_evidence") as mock:
        yield mock


def _ctx(**kwargs: Any) -> ToolContext:
    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "default",
    }
    base.update(kwargs)
    return ToolContext(**base)


def _rows(mock: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in mock.call_args_list]


# ── (a) a send whose id reads back ──────────────────────────────────────────


class TestReadBackConfirms:
    async def test_gmail_send_that_reads_back_records_verified_true(
        self, observe: None, evidence: Any
    ) -> None:
        sent = {"id": "msg-1", "threadId": "thread-1"}
        with patch.object(verification, "_gws_read", return_value={"id": "msg-1"}) as read:
            out = await verify_tool_result("gws_gmail_send", {"to": "a@example.com"}, sent, _ctx())

        assert read.await_count == 1, "verification must make exactly one read-back call"
        assert out == sent, "a verified result must reach the model unchanged"
        rows = _rows(evidence)
        assert len(rows) == 1
        assert rows[0]["kind"] == "tool_verify"
        assert rows[0]["verified"] is True
        assert "msg-1" in rows[0]["reference"]

    async def test_reply_uses_the_same_checker(self, observe: None, evidence: Any) -> None:
        with patch.object(verification, "_gws_read", return_value={"id": "msg-9"}):
            await verify_tool_result("gws_gmail_reply", {}, {"id": "msg-9"}, _ctx())
        assert _rows(evidence)[0]["verified"] is True


# ── (b) a send whose id does NOT read back ──────────────────────────────────


class TestReadBackFails:
    async def test_missing_message_records_verified_false(
        self, observe: None, evidence: Any
    ) -> None:
        sent = {"id": "msg-ghost", "threadId": "t"}
        with patch.object(verification, "_gws_read", return_value={"error": "Not Found"}):
            out = await verify_tool_result("gws_gmail_send", {}, sent, _ctx())

        assert out == sent, "observe mode must be side-effect-free apart from recording"
        assert "verification_failed" not in out
        row = _rows(evidence)[0]
        assert row["kind"] == "tool_verify"
        assert row["verified"] is False

    async def test_send_returning_no_id_is_unverifiable(self, observe: None, evidence: Any) -> None:
        with patch.object(verification, "_gws_read") as read:
            out = await verify_tool_result("gws_gmail_send", {}, {"output": "sent!"}, _ctx())
        assert read.await_count == 0, "no id means nothing to read back — spend no call"
        assert out == {"output": "sent!"}
        assert _rows(evidence)[0]["verified"] is False


# ── (c) enforce injects the failure into what the model sees ────────────────


class TestEnforceRung:
    async def test_enforce_injects_verification_failed(self, enforce: None, evidence: Any) -> None:
        with patch.object(verification, "_gws_read", return_value={"error": "Not Found"}):
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-ghost"}, _ctx())

        assert out["verification_failed"] is True
        assert out["id"] == "msg-ghost", "the original result must survive the injection"
        assert "gws_gmail_send" in out["verification_message"]
        assert _rows(evidence)[0]["verified"] is False

    async def test_enforce_leaves_a_verified_result_alone(
        self, enforce: None, evidence: Any
    ) -> None:
        with patch.object(verification, "_gws_read", return_value={"id": "msg-1"}):
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, _ctx())
        assert "verification_failed" not in out


class TestAlertRung:
    async def test_alert_notifies_the_operator_and_leaves_the_result_alone(
        self, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "alert")
        notify = MagicMock(return_value=True)
        monkeypatch.setattr("robothor.engine.feature_flags.notify_guardrail_alert", notify)
        with patch.object(verification, "_gws_read", return_value={"error": "Not Found"}):
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-ghost"}, _ctx())

        assert notify.call_count == 1, "the alert rung must actually reach the operator"
        assert notify.call_args.kwargs["guardrail_name"] == verification.GUARDRAIL_NAME
        assert "verification_failed" not in out, "alert observes, it does not act"

    async def test_alert_stays_quiet_when_the_read_back_confirms(
        self, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "alert")
        notify = MagicMock(return_value=True)
        monkeypatch.setattr("robothor.engine.feature_flags.notify_guardrail_alert", notify)
        with patch.object(verification, "_gws_read", return_value={"id": "msg-1"}):
            await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, _ctx())
        assert notify.call_count == 0

    async def test_a_dead_alert_channel_never_breaks_the_run(
        self, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_TOOL_VERIFY_MODE", "alert")
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert",
            MagicMock(side_effect=RuntimeError("telegram down")),
        )
        with patch.object(verification, "_gws_read", return_value={"error": "Not Found"}):
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-ghost"}, _ctx())
        assert out == {"id": "msg-ghost"}


# ── (d) a checker that raises must never touch the run ──────────────────────


class TestCheckerErrorsAreContained:
    async def test_raising_checker_records_verify_error(
        self, observe: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
            raise RuntimeError("read-back exploded")

        monkeypatch.setitem(POST_CONDITION_CHECKS, "gws_gmail_send", _boom)
        sent = {"id": "msg-1"}
        out = await verify_tool_result("gws_gmail_send", {}, sent, _ctx())

        assert out == sent, "a broken checker must never change the tool result"
        row = _rows(evidence)[0]
        assert row["kind"] == "verify_error"
        assert row["verified"] is False

    async def test_enforce_never_blocks_on_a_broken_checker(
        self, enforce: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
            raise RuntimeError("read-back exploded")

        monkeypatch.setitem(POST_CONDITION_CHECKS, "gws_gmail_send", _boom)
        out = await verify_tool_result("gws_gmail_send", {}, {"id": "m"}, _ctx())
        assert "verification_failed" not in out, "our own bug is not the agent's failure"

    async def test_slow_checker_times_out_without_hanging_the_run(
        self, observe: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _slow(args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
            await asyncio.sleep(10)

        monkeypatch.setitem(POST_CONDITION_CHECKS, "gws_gmail_send", _slow)
        monkeypatch.setattr(verification, "CHECK_TIMEOUT_SECONDS", 0.01)
        sent = {"id": "msg-1"}
        out = await verify_tool_result("gws_gmail_send", {}, sent, _ctx())

        assert out == sent
        assert _rows(evidence)[0]["kind"] == "verify_error"

    async def test_ledger_write_failure_is_swallowed(self, observe: None) -> None:
        """The evidence table may not exist yet (it ships in a sibling PR)."""
        with (
            patch.object(verification, "_gws_read", return_value={"id": "msg-1"}),
            patch(
                "robothor.db.connection.get_connection",
                side_effect=RuntimeError('relation "agent_run_evidence" does not exist'),
            ),
        ):
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, _ctx())
        assert out == {"id": "msg-1"}


# ── (e) the per-run budget ──────────────────────────────────────────────────


class TestPerRunBudget:
    async def test_cap_stops_further_checks(
        self, observe: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verification, "MAX_CHECKS_PER_RUN", 1)
        ctx = _ctx()
        with patch.object(verification, "_gws_read", return_value={"id": "msg-1"}) as read:
            await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, ctx)
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "msg-2"}, ctx)

        assert read.await_count == 1, "the budget must stop the second read-back"
        assert len(_rows(evidence)) == 1
        assert out == {"id": "msg-2"}

    async def test_budget_is_per_run(
        self, observe: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verification, "MAX_CHECKS_PER_RUN", 1)
        with patch.object(verification, "_gws_read", return_value={"id": "msg-1"}):
            await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, _ctx(run_id="run-a"))
            await verify_tool_result("gws_gmail_send", {}, {"id": "msg-1"}, _ctx(run_id="run-b"))
        assert len(_rows(evidence)) == 2


# ── (f) tools with no checker, and results nobody should check ──────────────


class TestUncheckedPaths:
    async def test_tool_without_a_checker_is_untouched(self, observe: None, evidence: Any) -> None:
        result = {"facts": ["a"]}
        out = await verify_tool_result("search_memory", {}, result, _ctx())
        assert out is result
        assert evidence.call_count == 0

    async def test_off_mode_does_nothing(self, evidence: Any) -> None:
        with patch.object(verification, "_gws_read") as read:
            out = await verify_tool_result("gws_gmail_send", {}, {"id": "m"}, _ctx())
        assert read.await_count == 0
        assert out == {"id": "m"}
        assert evidence.call_count == 0

    async def test_failed_tool_call_is_not_verified(self, observe: None, evidence: Any) -> None:
        result = {"error": "gws CLI not found"}
        out = await verify_tool_result("gws_gmail_send", {}, result, _ctx())
        assert out is result
        assert evidence.call_count == 0

    async def test_deduped_write_claims_no_new_side_effect(
        self, observe: None, evidence: Any
    ) -> None:
        result = {"status": "deduped", "existing_event_id": "evt-1"}
        out = await verify_tool_result("gws_calendar_create", {}, result, _ctx())
        assert out is result
        assert evidence.call_count == 0

    async def test_self_declared_failure_is_not_double_reported(
        self, observe: None, evidence: Any
    ) -> None:
        result = {"success": False, "id": "task-1"}
        out = await verify_tool_result("update_task", {}, result, _ctx())
        assert out is result
        assert evidence.call_count == 0


# ── CRM read-back checkers ──────────────────────────────────────────────────


class TestCrmCheckers:
    async def test_created_task_that_exists_verifies(self, observe: None, evidence: Any) -> None:
        with patch("robothor.crm.dal.get_task", return_value={"id": "t1", "title": "Pay Alice"}):
            await verify_tool_result("create_task", {"title": "Pay Alice"}, {"id": "t1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is True

    async def test_created_task_that_is_absent_fails(self, observe: None, evidence: Any) -> None:
        with patch("robothor.crm.dal.get_task", return_value=None):
            await verify_tool_result("create_task", {"title": "x"}, {"id": "t1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is False

    async def test_deduped_task_is_verified_on_existence_alone(
        self, observe: None, evidence: Any
    ) -> None:
        """Dedup hands back the pre-existing row — its title is not this call's."""
        existing = {"id": "t1", "title": "Filed earlier under another title"}
        with patch("robothor.crm.dal.get_task", return_value=existing):
            await verify_tool_result(
                "create_task",
                {"title": "Pay Alice"},
                {"id": "t1", "title": existing["title"], "deduplicated": True},
                _ctx(),
            )
        assert _rows(evidence)[0]["verified"] is True

    async def test_update_task_field_that_did_not_change_fails(
        self, observe: None, evidence: Any
    ) -> None:
        """The motivating incident: the record was never updated, but success was claimed."""
        row = {"id": "t1", "status": "TODO", "title": "Send the payment to Alice"}
        with patch("robothor.crm.dal.get_task", return_value=row):
            await verify_tool_result(
                "update_task", {"id": "t1", "status": "DONE"}, {"success": True, "id": "t1"}, _ctx()
            )
        rec = _rows(evidence)[0]
        assert rec["verified"] is False
        assert "status" in str(rec["detail"])

    async def test_update_task_field_that_changed_verifies(
        self, observe: None, evidence: Any
    ) -> None:
        row = {"id": "t1", "status": "DONE", "title": "Send the payment to Alice"}
        with patch("robothor.crm.dal.get_task", return_value=row):
            await verify_tool_result(
                "update_task", {"id": "t1", "status": "DONE"}, {"success": True, "id": "t1"}, _ctx()
            )
        assert _rows(evidence)[0]["verified"] is True

    async def test_explicit_null_field_is_not_a_mismatch(
        self, observe: None, evidence: Any
    ) -> None:
        """dal.update_task skips None fields, so a None request changed nothing."""
        row = {"id": "t1", "status": "DONE", "resolution": "closed out last week"}
        with patch("robothor.crm.dal.get_task", return_value=row):
            await verify_tool_result(
                "update_task",
                {"id": "t1", "status": "DONE", "resolution": None},
                {"success": True, "id": "t1"},
                _ctx(),
            )
        assert _rows(evidence)[0]["verified"] is True

    async def test_updated_person_field_that_did_not_change_fails(
        self, observe: None, evidence: Any
    ) -> None:
        row = {"id": "p1", "name": {"firstName": "Alice", "lastName": "Doe"}, "city": "Boston"}
        with patch("robothor.crm.dal.get_person", return_value=row):
            await verify_tool_result(
                "update_person",
                {"id": "p1", "city": "Denver"},
                {"success": True, "id": "p1"},
                _ctx(),
            )
        rec = _rows(evidence)[0]
        assert rec["verified"] is False
        assert "city" in str(rec["detail"])

    async def test_resolve_task_requires_done_status(self, observe: None, evidence: Any) -> None:
        with patch("robothor.crm.dal.get_task", return_value={"id": "t1", "status": "TODO"}):
            await verify_tool_result(
                "resolve_task", {"id": "t1"}, {"success": True, "id": "t1"}, _ctx()
            )
        assert _rows(evidence)[0]["verified"] is False

    async def test_notification_reads_back(self, observe: None, evidence: Any) -> None:
        with patch("robothor.crm.dal.get_notification", return_value={"id": "n1"}):
            await verify_tool_result("send_notification", {}, {"id": "n1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is True

    async def test_created_person_that_is_absent_fails(self, observe: None, evidence: Any) -> None:
        with patch("robothor.crm.dal.get_person", return_value=None):
            await verify_tool_result("create_person", {}, {"id": "p1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is False


class TestCalendarChecker:
    async def test_created_event_reads_back(self, observe: None, evidence: Any) -> None:
        with patch.object(verification, "_gws_read", return_value={"id": "evt-1"}):
            await verify_tool_result("gws_calendar_create", {}, {"id": "evt-1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is True

    async def test_cancelled_event_does_not_count(self, observe: None, evidence: Any) -> None:
        read = {"id": "evt-1", "status": "cancelled"}
        with patch.object(verification, "_gws_read", return_value=read):
            await verify_tool_result("gws_calendar_create", {}, {"id": "evt-1"}, _ctx())
        assert _rows(evidence)[0]["verified"] is False


# ── Registry + dispatch wiring ──────────────────────────────────────────────


class TestRegistry:
    def test_registry_covers_the_side_effecting_tools(self) -> None:
        for tool in (
            "gws_gmail_send",
            "gws_gmail_reply",
            "gws_calendar_create",
            "create_task",
            "update_task",
            "resolve_task",
            "create_person",
            "update_person",
            "send_notification",
        ):
            assert tool in POST_CONDITION_CHECKS, f"{tool} has no post-condition checker"

    def test_outcome_defaults_to_the_verify_kind(self) -> None:
        outcome = VerificationOutcome(reference="x", verified=True, detail={})
        assert outcome.kind == "tool_verify"


def _pin_handlers(monkeypatch, handlers):
    """Patch the dispatch handler map so it actually survives a lookup.

    `_get_handlers()` rebuilds the map whenever `_handler_map_generation`
    differs from the plugin loader's current generation. Patching only
    `_handler_map` leaves the counter at its initial -1, so the very next
    lookup throws the patch away and runs the REAL handler — which for
    `gws_gmail_send` means shelling out to `gws` and returning
    {"error": "gws exited with code 1"}.

    These tests passed anyway, because in a full-suite run some earlier test
    had already called `_get_handlers()` and left the counter warm. Run the
    file on its own and they failed. That is an order-dependent test in the
    file covering a verification control, so it was worth a fixture rather
    than a second patch line copied three times.
    """
    from robothor.engine.tools import dispatch
    from robothor.plugins import generation

    monkeypatch.setattr(dispatch, "_handler_map", dict(handlers))
    monkeypatch.setattr(dispatch, "_handler_map_generation", generation())


class TestDispatchWiring:
    async def test_execute_tool_verifies_after_a_successful_handler(
        self, observe: None, evidence: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.tools import dispatch

        async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return {"id": "msg-1"}

        _pin_handlers(monkeypatch, {"gws_gmail_send": _handler})
        with patch.object(verification, "_gws_read", return_value={"error": "Not Found"}):
            out = await dispatch._execute_tool(
                "gws_gmail_send",
                {"to": "a@example.com"},
                agent_id="main",
                run_id="11111111-1111-1111-1111-111111111111",
                tenant_id="default",
                user_role="service",
            )

        assert out == {"id": "msg-1"}, "observe mode must not alter the tool result"
        assert _rows(evidence)[0]["verified"] is False

    async def test_verification_failure_never_fails_the_tool_call(
        self, observe: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.tools import dispatch

        async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return {"id": "msg-1"}

        _pin_handlers(monkeypatch, {"gws_gmail_send": _handler})
        boom = MagicMock(side_effect=RuntimeError("verification subsystem is down"))
        monkeypatch.setattr(verification, "verify_tool_result", boom)
        out = await dispatch._execute_tool(
            "gws_gmail_send", {}, agent_id="main", tenant_id="default", user_role="service"
        )
        assert out == {"id": "msg-1"}
