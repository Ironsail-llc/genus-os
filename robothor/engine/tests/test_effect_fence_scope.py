# ruff: noqa: F811 - pytest fixtures arrive as parameters named like their imports
"""An unresolved write fences a repeat of the same action, not every later write.

A write whose outcome is unknown (the request reached the outside system, the
answer never came back) must never be sent again blind. Before, it also
refused every later call of the same tool by that principal, to any target,
for a day, with a message that did not say the refused call was never sent,
and nothing a plugin or an operator could do settled it short of a database
edit. These tests pin the narrower rule and the two supported ways out:

* the fence covers the same tool on the same TARGET (what the write acts on:
  a thread, a recipient), however the call is worded; a tool may declare the
  whole-tool scope instead;
* a refusal says plainly that nothing was sent, names the earlier action, and
  says how it is cleared;
* a plugin can register a read-back verifier for its write tool, and an
  operator can record an outcome with a reason; either way, later writes go on;
* an identical call is still never sent twice.
"""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.runtime import effect_tools, effects
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)

TOOL = "acme_post_message"


def _uncertain(ctx: Any, args: dict[str, Any], *, tool: str = TOOL, run: str = "worker"):
    row = effects.begin(ctx, run, "main", tool, args)
    assert effects.mark_dispatched(ctx, row["id"], run)
    assert effects.finish(ctx, row["id"], run, uncertain=True)
    return row


def _begin(ctx: Any, args: dict[str, Any], *, tool: str = TOOL) -> dict[str, Any]:
    return effects.begin(ctx, "worker", "main", tool, args)


@pytest.fixture
def declared(monkeypatch):
    """The effect declarations installed plugins make, set per test."""
    specs: dict[str, effect_tools.EffectSpec] = {}
    monkeypatch.setattr(effect_tools, "plugin_specs", lambda: specs)
    return specs


def _later(ctx: Any) -> Any:
    return replace(ctx, request_id="later-request")


# ── criterion 2: the scope is the same tool on the same target ───────────


class TestScope:
    def test_another_target_is_not_fenced(self, effect_db, declared):
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        row = _begin(_later(ctx), {"thread_id": "thread-2", "text": "hello"})
        assert row["state"] == "prepared"

    def test_the_same_target_reworded_is_still_fenced(self, effect_db, declared):
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        with pytest.raises(effects.EffectPendingError):
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "hello again, reworded"})

    def test_another_tool_is_not_fenced(self, effect_db, declared):
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        row = _begin(_later(ctx), {"title": "follow up"}, tool="acme_create_ticket")
        assert row["state"] == "prepared"

    def test_declared_target_keys_decide_the_target(self, effect_db, declared):
        # Only thread_id names what the write acts on; a formatting flag does not.
        declared[TOOL] = effect_tools.EffectSpec(target=("thread_id",))
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hi", "markdown": True})
        with pytest.raises(effects.EffectPendingError):
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "hi", "markdown": False})
        assert _begin(_later(ctx), {"thread_id": "thread-2", "text": "hi"})["state"] == "prepared"

    def test_a_tool_can_declare_the_whole_tool_scope(self, effect_db, declared):
        declared[TOOL] = effect_tools.EffectSpec(fence="tool")
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        with pytest.raises(effects.EffectPendingError):
            _begin(_later(ctx), {"thread_id": "thread-2", "text": "hello"})

    def test_a_row_written_before_targets_were_recorded_keeps_the_wide_fence(
        self, effect_db, declared
    ):
        # Upgrade safety: an uncertain row with no recorded target cannot be
        # told apart from this one, so it fences the tool as it did before.
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        with effect_db() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_runtime_effects SET target=NULL,target_key=NULL WHERE id=%s",
                (row["id"],),
            )
        with pytest.raises(effects.EffectPendingError):
            _begin(_later(ctx), {"thread_id": "thread-2", "text": "hello"})

    def test_an_identical_call_is_never_sent_twice(self, effect_db, declared):
        ctx = context()
        args = {"thread_id": "thread-1", "text": "hello"}
        _uncertain(ctx, args)
        for other in (ctx, _later(ctx), replace(ctx, request_id="much-later")):
            with pytest.raises(effects.EffectPendingError) as raised:
                _begin(other, args)
            assert raised.value.same_action is True


# ── criterion 1: the refusal says nothing was sent ───────────────────────


class TestRefusal:
    def test_it_says_this_write_was_not_sent_and_names_the_earlier_action(
        self, effect_db, declared
    ):
        ctx = context()
        earlier = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        with pytest.raises(effects.EffectPendingError) as raised:
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "second message"})
        err = raised.value
        text = str(err)
        assert err.not_sent is True
        assert err.same_action is False
        assert err.effect_id == str(earlier["id"])
        assert "NOT sent" in text
        assert TOOL in text and "thread_id=thread-1" in text
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", text)
        # How it is cleared: the operator verb, naming this effect.
        assert f"genus effects resolve {earlier['id']}" in text
        # No verifier and no goal: the day-long release is named too.
        assert "24 hours" in text

    def test_a_tool_with_a_verifier_says_the_read_back_clears_it(self, effect_db, declared):
        declared[TOOL] = effect_tools.EffectSpec(
            verify=lambda record: effects.Verification("unknown")
        )
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        with pytest.raises(effects.EffectPendingError) as raised:
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "again"})
        assert "read-back" in str(raised.value)
        assert "24 hours" not in str(raised.value)

    def test_the_refusal_does_not_carry_the_target_values_it_does_not_need(
        self, effect_db, declared
    ):
        # The summary names the target, never the wording that was refused.
        ctx = context()
        _uncertain(ctx, {"thread_id": "thread-1", "text": "private wording"})
        with pytest.raises(effects.EffectPendingError) as raised:
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "other private wording"})
        assert "private wording" not in str(raised.value)


# ── criteria 3 and 4: a read-back verifier settles it ────────────────────


class TestVerifier:
    def _settle(self, declared, verdict):
        declared[TOOL] = effect_tools.EffectSpec(target=("thread_id",), verify=lambda r: verdict)
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        scope = SimpleNamespace(tenant_id=ctx.tenant_id, principal_id=ctx.principal_id)
        return ctx, row, effect_tools.recover(scope, row["id"])

    def test_applied_unblocks_later_writes_and_replays_the_original(self, effect_db, declared):
        verdict = effects.Verification("applied", True, "acme:message-9", {"id": "message-9"})
        ctx, row, recovered = self._settle(declared, verdict)
        assert recovered is not None and recovered["id"] == "message-9"
        assert effects.read(ctx, row["id"])["state"] == "confirmed"
        assert _begin(_later(ctx), {"thread_id": "thread-1", "text": "next"})["state"] == (
            "prepared"
        )
        # The identical call in its own request is answered, not sent again.
        replayed = _begin(ctx, {"thread_id": "thread-1", "text": "hello"})
        assert replayed["state"] == "confirmed" and replayed["id"] == row["id"]

    def test_not_applied_unblocks_later_writes(self, effect_db, declared):
        verdict = effects.Verification("not_applied", True, "acme:no-such-message")
        ctx, row, recovered = self._settle(declared, verdict)
        assert recovered is None
        assert effects.read(ctx, row["id"])["state"] == "not_applied"
        assert _begin(_later(ctx), {"thread_id": "thread-1", "text": "next"})["state"] == (
            "prepared"
        )

    def test_an_unknown_read_back_keeps_the_fence(self, effect_db, declared):
        ctx, row, recovered = self._settle(declared, effects.Verification("unknown"))
        assert recovered is None
        with pytest.raises(effects.EffectPendingError):
            _begin(_later(ctx), {"thread_id": "thread-1", "text": "next"})

    def test_a_verifier_that_raises_keeps_the_fence(self, effect_db, declared):
        def broken(record):
            raise ConnectionError("provider down")

        declared[TOOL] = effect_tools.EffectSpec(verify=broken)
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        scope = SimpleNamespace(tenant_id=ctx.tenant_id, principal_id=ctx.principal_id)
        assert effect_tools.recover(scope, row["id"]) is None
        assert effects.read(ctx, row["id"])["state"] == "uncertain"

    def test_the_verifier_reads_the_recorded_target(self, effect_db, declared):
        seen: list[dict[str, Any]] = []

        def verify(record):
            seen.append(record)
            return effects.Verification("unknown")

        declared[TOOL] = effect_tools.EffectSpec(target=("thread_id",), verify=verify)
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        effect_tools.recover(
            SimpleNamespace(tenant_id=ctx.tenant_id, principal_id=ctx.principal_id), row["id"]
        )
        assert seen and seen[0]["target"] == {"thread_id": "thread-1"}
        assert str(seen[0]["id"]) == str(row["id"])

    def test_a_tool_with_a_verifier_is_never_expired_unverified(self, effect_db, declared):
        declared[TOOL] = effect_tools.EffectSpec(verify=lambda r: effects.Verification("unknown"))
        assert effect_tools.has_verifier(TOOL)
        assert effect_tools.has_verifier("create_task")
        assert not effect_tools.has_verifier("acme_other_tool")

    def test_the_sweep_runs_plugin_verifiers(self, effect_db, declared, monkeypatch):
        from robothor.engine.runtime import effect_recovery

        verdict = effects.Verification("applied", True, "acme:message-9", {"id": "message-9"})
        declared[TOOL] = effect_tools.EffectSpec(verify=lambda r: verdict)
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        effect_recovery.sweep_terminal(ctx.tenant_id)
        assert effects.read(ctx, row["id"])["state"] == "confirmed"


# ── criterion 3: an operator records the outcome with a reason ───────────


class TestOperator:
    def test_unresolved_lists_one_tenants_open_effects(self, effect_db, declared):
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        _uncertain(replace(ctx, tenant_id="another-tenant"), {"thread_id": "thread-1", "t": 1})
        listed = effects.unresolved(ctx.tenant_id)
        assert [str(item["id"]) for item in listed] == [str(row["id"])]
        assert listed[0]["tool_name"] == TOOL
        assert listed[0]["target"] == {"thread_id": "thread-1"}
        assert listed[0]["principal_id"] == ctx.principal_id

    @pytest.mark.parametrize(
        ("outcome", "state"), [("applied", "finished"), ("not_applied", "not_applied")]
    )
    def test_a_recorded_outcome_unblocks_later_writes(self, effect_db, declared, outcome, state):
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        assert effects.operator_resolve(
            ctx.tenant_id, row["id"], outcome=outcome, actor="operator:alice", reason="checked"
        )
        record = effects.read(ctx, row["id"])
        assert record["state"] == state
        assert record["resolution"]["source"] == "operator"
        assert record["resolution"]["outcome"] == outcome
        assert record["resolution"]["note"] == "checked"
        assert record["resolution"]["actor"] == "operator:alice"
        assert record["resolution"]["verified"] is False
        assert _begin(_later(ctx), {"thread_id": "thread-1", "text": "next"})["state"] == (
            "prepared"
        )

    def test_applied_by_the_operator_is_not_sent_again_in_its_own_request(
        self, effect_db, declared
    ):
        ctx = context()
        args = {"thread_id": "thread-1", "text": "hello"}
        row = _uncertain(ctx, args)
        effects.operator_resolve(
            ctx.tenant_id, row["id"], outcome="applied", actor="operator:alice", reason="seen"
        )
        replayed = _begin(ctx, args)
        assert replayed["id"] == row["id"] and replayed["state"] == "finished"

    def test_a_reason_and_an_actor_are_required(self, effect_db, declared):
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        for actor, reason in (("operator:alice", " "), ("", "checked")):
            with pytest.raises(ValueError):
                effects.operator_resolve(
                    ctx.tenant_id, row["id"], outcome="applied", actor=actor, reason=reason
                )
        with pytest.raises(ValueError):
            effects.operator_resolve(
                ctx.tenant_id, row["id"], outcome="maybe", actor="operator:alice", reason="x"
            )
        assert effects.read(ctx, row["id"])["state"] == "uncertain"

    def test_another_tenant_cannot_resolve_it(self, effect_db, declared):
        ctx = context()
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        assert not effects.operator_resolve(
            "another-tenant", row["id"], outcome="applied", actor="operator:bob", reason="no"
        )
        assert effects.read(ctx, row["id"])["state"] == "uncertain"

    def test_a_goal_owned_effect_is_left_to_its_goal(self, effect_db, declared):
        ctx = replace(context(), goal_id="goal", attempt_id="attempt", budget_id="family")
        row = _uncertain(ctx, {"thread_id": "thread-1", "text": "hello"})
        assert not effects.operator_resolve(
            ctx.tenant_id, row["id"], outcome="applied", actor="operator:alice", reason="x"
        )
        assert effects.read(ctx, row["id"])["state"] == "uncertain"


# ── what the model is told ───────────────────────────────────────────────


@pytest.fixture
def dispatcher(effect_db, declared, monkeypatch):
    from robothor.engine.runtime.current import active_context
    from robothor.engine.tools import dispatch

    ctx = context()
    sent: list[dict[str, Any]] = []

    async def post(args, tool_context):
        sent.append(args)
        if args.get("text") == "lost":
            return {"error": "connection dropped", "outcome_unknown": True, "retryable": False}
        return {"ok": True, "message_id": f"m{len(sent)}"}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {TOOL: post})
    monkeypatch.setattr(
        "robothor.engine.tools.get_registry",
        lambda: SimpleNamespace(get_adapter_route=lambda name: None),
    )
    monkeypatch.setattr(
        "robothor.engine.tools.read_only.declared_read_only_tools", lambda: frozenset()
    )

    async def call(args, *, request="request"):
        scope = replace(ctx, request_id=request)
        token = active_context.set(scope)
        try:
            return await dispatch._execute_tool(
                TOOL,
                args,
                agent_id="main",
                run_id="worker",
                tenant_id=scope.tenant_id,
                user_id=scope.principal_id,
                user_role="service",
            )
        finally:
            active_context.reset(token)

    return call, sent


class TestWhatTheModelIsTold:
    async def test_a_refused_new_message_says_it_was_not_sent(self, dispatcher):
        call, sent = dispatcher
        first = await call({"thread_id": "thread-1", "text": "lost"})
        assert first["outcome_unknown"] and len(sent) == 1
        refused = await call({"thread_id": "thread-1", "text": "a new message"}, request="later")
        assert len(sent) == 1
        assert refused["not_sent"] is True
        assert "outcome_unknown" not in refused  # its own outcome is known: nothing went out
        assert refused["blocked_by"]["effect_id"] == first["effect_id"]
        assert refused["blocked_by"]["tool"] == TOOL
        assert "NOT sent" in refused["error"]

    async def test_another_thread_goes_out(self, dispatcher):
        call, sent = dispatcher
        await call({"thread_id": "thread-1", "text": "lost"})
        result = await call({"thread_id": "thread-2", "text": "hello"}, request="later")
        assert result["ok"] is True and len(sent) == 2

    async def test_an_identical_repeat_still_reports_the_earlier_unknown(self, dispatcher):
        call, sent = dispatcher
        first = await call({"thread_id": "thread-1", "text": "lost"})
        again = await call({"thread_id": "thread-1", "text": "lost"}, request="later")
        assert len(sent) == 1
        assert again["not_sent"] is True
        assert again["outcome_unknown"] and again["effect_id"] == first["effect_id"]
