"""Running a workspace outside interactive chat, against real Postgres.

``robothor.rooms.runs`` is the one path that runs a room's agent for a
schedule, a "run now", a preview or a period. The runner is faked (it plays
the agent: it publishes through the real ``room_publish_edition`` code with
whatever run carrier it was given); everything else is real.

Stories:
- A2 preview: an edition only its requester sees; no notification, no task,
  no event, no fan-out; a failed preview tells nobody else.
- A5 pause: a paused room's schedule does not run; run now still works for an
  assigned user; resume computes the next run from now.
- B4 failure: the edition is failed with a reason, assigned users only are
  told, the room pauses itself after ``AUTO_PAUSE_AFTER_FAILURES`` in a row,
  and a good run clears the count.
- B5 cost cap: the room's ``max_cost_usd`` caps the run; a capped run's
  edition is failed "stopped at the cost cap", never published.
- B6 run now: ``run`` access; a viewer is refused; a run in progress is joined.
- B7 one run: a fire during a live chat turn waits for the room's lock; two
  triggers for the same period make one run; the agent being busy elsewhere
  does not skip the room.
- B8 run-as: re-read at fire time; a deactivated account pauses the room and
  tells the tenant's admins; a reduced role runs reduced.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.rooms import dal, definition, editions, fanout, runs
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden
from robothor.rooms.run_context import current_room_run
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401

pytestmark = pytest.mark.integration

BLOCKS = [{"component": "render_metric_card", "props": {"title": "Revenue", "value": 1200}}]


class FakeRunner:
    """Plays the agent. ``mode`` decides what the run does."""

    def __init__(self, cur: Any = None) -> None:
        self.cur = cur
        self.config = SimpleNamespace(manifest_dir="/nonexistent", tenant_id="")
        self.calls: list[dict[str, Any]] = []
        self.carriers: list[Any] = []
        self.mode = "publish"
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def execute(self, **kw: Any) -> AgentRun:
        self.calls.append(kw)
        carrier = current_room_run()
        self.carriers.append(carrier)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Sales are up 4%.",
            trigger_type=kw.get("trigger_type", TriggerType.CRON),
            agent_id=kw.get("agent_id", ""),
        )
        if self.mode == "raise":
            raise RuntimeError("provider down")
        if self.mode == "fail":
            run.status = RunStatus.FAILED
            run.error_message = "model error"
            return run
        if self.mode == "timeout":
            run.status = RunStatus.TIMEOUT
            run.error_message = "hard timeout"
            return run
        if self.mode == "capped":
            run.budget_exhausted = True
            return run
        if self.mode == "publish":
            # The real runner records the run; the publish refuses one with no row.
            self.cur.execute(
                "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, status, "
                "started_at, user_id) VALUES (%s, %s, %s, 'cron', 'running', now(), %s)",
                (run.id, kw["tenant_id"], kw["agent_id"], kw["user_id"]),
            )
            result = editions.publish_from_run(
                {
                    "room_id": carrier.room_id if carrier else "",
                    "title": "Daily sales",
                    "summary": "Up 4% on the day.",
                    "blocks": BLOCKS,
                },
                tenant_id=kw["tenant_id"],
                agent_id=kw["agent_id"],
                run_id=run.id,
                user_id=kw["user_id"],
                user_role=kw["user_role"],
                run=carrier,
            )
            assert "error" not in result, result
        return run


@pytest.fixture
def env(staff_db, monkeypatch):  # noqa: F811
    from robothor.crm import dal as crm_dal
    from robothor.engine import chat_store

    for module in (crm_dal, chat_store, editions, runs):
        monkeypatch.setattr(module, "get_connection", committed_connection)
    events: list[str] = []
    monkeypatch.setattr(editions, "emit_edition_event", lambda r, e: events.append(e.id))
    fanouts: list[tuple[str, dict[str, Any]]] = []

    async def fake_fan_out(room: Any, edition: Any, **kw: Any) -> list[Any]:
        fanouts.append((edition.id, kw))
        return []

    monkeypatch.setattr(fanout, "fan_out_edition", fake_fan_out)
    monkeypatch.setattr(runs, "_agent_config", lambda agent_id: AgentConfig(id=agent_id, name="An"))
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: "1-0")
    runner = FakeRunner(staff_db.cur)
    runs.bind_runner(runner)
    try:
        yield SimpleNamespace(db=staff_db, runner=runner, events=events, fanouts=fanouts)
    finally:
        runs.bind_runner(None)
        for table in ("crm_agent_notifications", "agent_runs"):
            staff_db.cur.execute(
                f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",  # noqa: S608
                ([staff_db.tenant, staff_db.other],),
            )


def _actor(db: Any, name: str) -> Actor:
    role = {"vera": "viewer", "dana": "admin", "owen": "owner"}.get(name, "member")
    return Actor(user_id=getattr(db, name), role=role, tenant_id=db.tenant)


def _report(db: Any, **kw: Any) -> Any:
    args = {
        "kind": "report",
        "title": "Daily sales",
        "instructions": "Summarise yesterday's sales.",
        "agent_id": "main",
        "schedule_text": "0 7 * * *",
        "default_grain": "day",
        "share_with": [db.bob, db.vera],
    }
    args.update(kw)
    return definition.create_room_with_schedule(_actor(db, "alice"), **args)


def _notes(db: Any) -> list[dict[str, Any]]:
    db.cur.execute(
        "SELECT to_agent, notification_type, subject, body, metadata "
        "FROM crm_agent_notifications WHERE tenant_id = %s ORDER BY created_at",
        (db.tenant,),
    )
    cols = ("to", "type", "subject", "body", "metadata")
    return [dict(zip(cols, row, strict=True)) for row in db.cur.fetchall()]


def _room_tasks(db: Any) -> int:
    db.cur.execute("SELECT COUNT(*) FROM room_tasks WHERE tenant_id = %s", (db.tenant,))
    return int(db.cur.fetchone()[0])


# ─── the period helper (replaced by robothor.rooms.periods at integration) ──


class TestLastCompletePeriod:
    def test_a_day_is_yesterday_in_the_rooms_zone(self):
        now = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
        assert runs.last_complete_period("day", "UTC", now) == runs.Period(
            date(2026, 11, 20), date(2026, 11, 20), "day"
        )
        # 03:00 UTC is still the 20th in Los Angeles, so the 19th is the last whole day.
        la = runs.last_complete_period("day", "America/Los_Angeles", now)
        assert (la.start, la.end) == (date(2026, 11, 19), date(2026, 11, 19))

    def test_a_week_runs_monday_to_sunday(self):
        now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)  # a Saturday
        week = runs.last_complete_period("week", "UTC", now)
        assert (week.start, week.end, week.grain) == (date(2026, 11, 9), date(2026, 11, 15), "week")

    def test_a_month_is_the_last_whole_month(self):
        now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        month = runs.last_complete_period("month", "UTC", now)
        assert (month.start, month.end) == (date(2026, 10, 1), date(2026, 10, 31))


# ─── a scheduled run ──────────────────────────────────────────────────────


class TestScheduledRun:
    async def test_it_runs_as_the_run_as_user_bound_to_its_edition(self, env):
        room = _report(env.db)

        edition = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert edition is not None and edition.status == "published"
        call = env.runner.calls[0]
        assert call["user_id"] == env.db.alice
        assert call["user_role"] == "member"
        assert call["tenant_id"] == env.db.tenant
        assert call["trigger_detail"] == f"room:{room.id}"
        assert call["trigger_type"] == TriggerType.CRON
        carrier = env.runner.carriers[0]
        assert carrier.room_id == room.id and carrier.session_key == room.session_key
        assert carrier.edition_id == edition.id and carrier.preview is False
        assert carrier.grain == "day" and carrier.period_end == carrier.period_start
        assert env.fanouts and env.fanouts[0][0] == edition.id

    async def test_the_instructions_reach_the_agent_and_history_is_bounded(self, env):
        room = _report(env.db)
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice
        )

        second = env.runner.calls[-1]
        assert "Summarise yesterday's sales." in second["message"]
        history = second["conversation_history"]
        assert 0 < len(history) <= runs.HISTORY_LIMIT
        assert any("Sales are up 4%." in str(h.get("content")) for h in history)

    async def test_the_last_edition_reaches_the_agent_as_data_only(self, env):
        room = _report(env.db)
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice
        )

        message = env.runner.calls[-1]["message"]
        assert "Up 4% on the day." in message
        assert "<untrusted_content" in message

    async def test_the_exchange_is_saved_into_the_room_attributed_to_the_agent(self, env):
        room = _report(env.db)

        edition = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        messages = dal.recent_messages(env.db.tenant, room.id)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == "Sales are up 4%."
        env.db.cur.execute(
            "SELECT message FROM chat_messages cm JOIN chat_sessions s ON s.id = cm.session_id "
            "WHERE s.tenant_id = %s AND s.session_key = %s ORDER BY cm.id",
            (env.db.tenant, room.session_key),
        )
        user_row, agent_row = (r[0] for r in env.db.cur.fetchall())
        assert agent_row.get("author_agent_id") == "main"
        assert user_row.get("room_trigger") == "schedule"
        assert "author_user_id" not in user_row
        stamped = dal.get_edition(env.db.tenant, edition.id, viewer_id=None)
        assert stamped.chat_message_id is not None
        assert dal.get_room(env.db.tenant, room.id).last_message_id is not None

    async def test_a_paused_room_does_not_run_on_schedule(self, env):
        room = _report(env.db)
        dal.set_paused(env.db.tenant, room.id, True)

        assert await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule") is None
        assert env.runner.calls == []

    async def test_an_archived_room_never_runs(self, env):
        room = _report(env.db)
        dal.archive_room(env.db.tenant, room.id)

        for trigger in ("schedule", "manual"):
            result = await runs.execute_room_run(
                env.db.tenant, room.id, trigger=trigger, requested_by=env.db.alice
            )
            assert result is None
        assert env.runner.calls == []

    async def test_an_unknown_trigger_is_refused(self, env):
        room = _report(env.db)
        with pytest.raises(ValueError):
            await runs.execute_room_run(env.db.tenant, room.id, trigger="hourly")

    async def test_a_room_of_another_tenant_is_not_run(self, env):
        room = _report(env.db)
        assert await runs.execute_room_run(env.db.other, room.id, trigger="schedule") is None
        assert env.runner.calls == []


# ─── B7: exactly one run ──────────────────────────────────────────────────


class TestOneRun:
    async def test_a_fire_during_a_live_chat_turn_waits_for_the_lock(self, env):
        room = _report(env.db)
        lock = fanout.room_lock(room.id)
        await lock.acquire()  # a chat turn holds the room

        task = asyncio.create_task(
            runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        )
        await asyncio.sleep(0.3)
        assert env.runner.calls == []  # waiting, not dropped
        lock.release()

        edition = await asyncio.wait_for(task, 10)
        assert edition is not None and edition.status == "published"

    async def test_two_triggers_for_the_same_period_make_one_run(self, env):
        room = _report(env.db)
        env.runner.gate = asyncio.Event()

        first = asyncio.create_task(
            runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        )
        await asyncio.wait_for(env.runner.entered.wait(), 10)
        second = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        env.runner.gate.set()
        done = await asyncio.wait_for(first, 10)

        assert second is None
        assert len(env.runner.calls) == 1
        assert done.status == "published"

    async def test_the_agent_busy_elsewhere_does_not_skip_the_room(self, env):
        from robothor.engine.dedup import release, try_acquire

        room = _report(env.db)
        assert await try_acquire("main")  # the agent's own cron is running
        try:
            edition = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        finally:
            await release("main")

        assert edition is not None and edition.status == "published"

    async def test_chat_is_told_the_room_is_busy_during_a_run(self, env):
        from robothor.engine import chat_rooms

        room = _report(env.db)
        env.runner.gate = asyncio.Event()
        task = asyncio.create_task(
            runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        )
        await asyncio.wait_for(env.runner.entered.wait(), 10)

        refusal = chat_rooms.busy(chat_rooms.ChatTarget(room.session_key, room))

        env.runner.gate.set()
        await asyncio.wait_for(task, 10)
        assert refusal is not None and refusal.status_code == 409
        assert runs.active_run(room.id) is None


# ─── B8: run-as re-resolved at fire time ──────────────────────────────────


class TestRunAs:
    async def test_a_deactivated_run_as_pauses_the_room_and_tells_admins(self, env):
        room = _report(env.db)
        env.db.cur.execute(
            "UPDATE user_accounts SET status = 'disabled' WHERE id = %s", (env.db.alice,)
        )

        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert result is None
        assert env.runner.calls == []
        assert dal.get_room(env.db.tenant, room.id).paused is True
        notes = _notes(env.db)
        assert {n["to"] for n in notes} == {env.db.dana, env.db.owen}
        assert all(n["metadata"].get("room_id") == room.id for n in notes)
        assert all(room.session_key not in str(n) for n in notes)
        assert definition.room_schedule(env.db.tenant, room.id)["enabled"] is False

    async def test_a_run_as_who_can_no_longer_post_pauses_the_room(self, env):
        room = _report(env.db)
        env.db.cur.execute(
            "UPDATE user_accounts SET role = 'viewer' WHERE id = %s", (env.db.alice,)
        )

        assert await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule") is None
        assert dal.get_room(env.db.tenant, room.id).paused is True
        assert env.runner.calls == []

    async def test_a_reduced_role_is_used_as_reduced(self, env):
        env.db.cur.execute("UPDATE user_accounts SET role = 'admin' WHERE id = %s", (env.db.alice,))
        room = _report(env.db)
        env.db.cur.execute(
            "UPDATE user_accounts SET role = 'member' WHERE id = %s", (env.db.alice,)
        )

        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert env.runner.calls[0]["user_role"] == "member"

    async def test_reassigning_and_resuming_runs_again(self, env):
        room = _report(env.db)
        env.db.cur.execute(
            "UPDATE user_accounts SET status = 'disabled' WHERE id = %s", (env.db.alice,)
        )
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        dana = _actor(env.db, "dana")
        definition.reassign_room(dana, room.id, env.db.bob)
        current = dal.get_room(env.db.tenant, room.id)
        definition.apply_definition_change(dana, room.id, current.rev, {"paused": False})

        edition = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert edition is not None and env.runner.calls[-1]["user_id"] == env.db.bob


# ─── B4: failures ─────────────────────────────────────────────────────────


class TestFailure:
    @pytest.mark.parametrize("mode", ["raise", "fail", "nothing"])
    async def test_a_failed_run_fails_its_edition_with_a_reason(self, env, mode):
        room = _report(env.db)
        env.runner.mode = mode

        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert result is not None and result.status == "failed"
        assert result.error
        assert env.fanouts == []
        assert dal.get_room(env.db.tenant, room.id).consecutive_failures == 1

    async def test_only_assigned_users_are_told(self, env):
        room = _report(env.db)
        env.runner.mode = "raise"

        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        notes = _notes(env.db)
        assert [n["to"] for n in notes] == [env.db.alice]
        assert notes[0]["metadata"]["room_id"] == room.id
        assert room.session_key not in str(notes)

    async def test_the_last_good_edition_stays_the_latest(self, env):
        room = _report(env.db)
        good = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        env.runner.mode = "raise"
        await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice
        )

        (summary,) = dal.list_rooms_for_user(env.db.tenant, env.db.bob)
        assert summary.latest_edition["id"] == good.id

    async def test_consecutive_failures_pause_the_room_and_say_why(self, env):
        room = _report(env.db)
        env.runner.mode = "raise"
        for day in range(runs.AUTO_PAUSE_AFTER_FAILURES):
            period = runs.Period(date(2026, 11, 1 + day), date(2026, 11, 1 + day), "day")
            await runs.execute_room_run(
                env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice, period=period
            )

        assert dal.get_room(env.db.tenant, room.id).paused is True
        assert definition.room_schedule(env.db.tenant, room.id)["enabled"] is False
        last = _notes(env.db)[-1]
        assert last["to"] == env.db.alice
        assert "paused" in (last["subject"] + (last["body"] or "")).lower()

    async def test_a_good_run_clears_the_failure_count(self, env):
        room = _report(env.db)
        env.runner.mode = "raise"
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        env.runner.mode = "publish"

        await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice
        )

        assert dal.get_room(env.db.tenant, room.id).consecutive_failures == 0

    async def test_unsafe_instructions_are_refused_at_fire_time(self, env, monkeypatch):
        room = _report(env.db)
        from robothor.engine import cron_safety

        monkeypatch.setattr(
            cron_safety, "scan_assembled_cron_prompt", lambda text: "matched pattern: x"
        )

        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert env.runner.calls == []
        assert result is not None and result.status == "failed"
        assert "injection" in (result.error or "")
        assert dal.get_room(env.db.tenant, room.id).paused is True


# ─── every exit ends the edition ──────────────────────────────────────────


def _building(db: Any, room_id: str) -> list[Any]:
    return [
        e
        for e in dal.list_editions(db.tenant, room_id, viewer_id=db.alice, include_unpublished=True)
        if e.status == "building"
    ]


class TestEveryExitEndsTheEdition:
    async def test_a_cancelled_run_fails_its_edition(self, env):
        room = _report(env.db)
        env.runner.gate = asyncio.Event()
        task = asyncio.create_task(
            runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        )
        await asyncio.wait_for(env.runner.entered.wait(), 10)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _building(env.db, room.id) == []
        (edition,) = dal.list_editions(
            env.db.tenant, room.id, viewer_id=None, include_unpublished=True
        )
        assert edition.status == "failed" and "stopped" in (edition.error or "")
        assert runs.active_run(room.id) is None
        assert not fanout.room_lock(room.id).locked()

    async def test_shutdown_cancelling_pending_runs_fails_their_editions(self, env):
        room = _report(env.db)
        env.runner.gate = asyncio.Event()
        edition, _ = await runs.start_room_run(_actor(env.db, "alice"), room.id)
        await asyncio.wait_for(env.runner.entered.wait(), 10)

        for task in runs.pending_runs():
            task.cancel()
        await asyncio.gather(*runs.pending_runs(), return_exceptions=True)

        assert dal.get_edition(env.db.tenant, edition.id, viewer_id=None).status == "failed"

    async def test_a_timed_out_run_fails_its_edition(self, env):
        room = _report(env.db)
        env.runner.mode = "timeout"
        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        assert result.status == "failed" and "timeout" in (result.error or "")

    async def test_an_error_before_the_agent_starts_fails_its_edition(self, env, monkeypatch):
        room = _report(env.db)

        def broken(agent_id: str) -> AgentConfig:
            raise RuntimeError("manifest unreadable")

        monkeypatch.setattr(runs, "_agent_config", broken)

        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert result.status == "failed"
        assert _building(env.db, room.id) == []
        assert not fanout.room_lock(room.id).locked()


# ─── B5: the cost cap ─────────────────────────────────────────────────────


class TestCostCap:
    async def test_the_rooms_cap_is_the_runs_hard_budget(self, env):
        room = _report(env.db)
        dal.update_definition(
            env.db.tenant, room.id, {"settings": {"max_cost_usd": 0.5}}, env.db.alice, room.rev
        )

        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        config = env.runner.calls[0]["agent_config"]
        assert config.hard_budget is True and config.max_cost_usd == 0.5

    async def test_a_room_with_no_cap_gets_the_platform_default(self, env):
        room = _report(env.db)
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        config = env.runner.calls[0]["agent_config"]
        assert config.hard_budget is True
        assert config.max_cost_usd == runs.DEFAULT_MAX_COST_USD

    async def test_the_agents_own_lower_cap_wins(self, env, monkeypatch):
        monkeypatch.setattr(
            runs,
            "_agent_config",
            lambda agent_id: AgentConfig(
                id=agent_id, name="An", hard_budget=True, max_cost_usd=0.1
            ),
        )
        room = _report(env.db)
        await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")
        assert env.runner.calls[0]["agent_config"].max_cost_usd == 0.1

    async def test_a_capped_run_is_never_published_as_complete(self, env):
        room = _report(env.db)
        env.runner.mode = "capped"

        result = await runs.execute_room_run(env.db.tenant, room.id, trigger="schedule")

        assert result.status == "failed"
        assert result.error == runs.CAPPED_REASON
        assert env.fanouts == []


# ─── A2: preview ──────────────────────────────────────────────────────────


class TestPreview:
    async def test_a_preview_is_its_requesters_only_and_tells_nobody(self, env):
        room = _report(env.db)

        edition = await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice, preview=True
        )

        assert edition.preview is True and edition.status == "published"
        assert env.runner.carriers[0].preview is True
        assert env.runner.carriers[0].requested_by == env.db.alice
        assert dal.get_edition(env.db.tenant, edition.id, viewer_id=env.db.bob) is None
        assert dal.list_editions(env.db.tenant, room.id, viewer_id=env.db.bob) == []
        assert _notes(env.db) == []
        assert _room_tasks(env.db) == 0
        assert env.events == [] and env.fanouts == []
        # Nothing of the preview lands in the shared conversation.
        assert dal.recent_messages(env.db.tenant, room.id) == []

    async def test_a_failed_preview_tells_nobody_and_counts_nothing(self, env):
        room = _report(env.db)
        env.runner.mode = "raise"

        edition = await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice, preview=True
        )

        assert edition.status == "failed" and edition.error
        assert dal.get_edition(env.db.tenant, edition.id, viewer_id=env.db.alice) is not None
        assert _notes(env.db) == []
        assert dal.get_room(env.db.tenant, room.id).consecutive_failures == 0

    async def test_a_preview_is_capped_too(self, env):
        room = _report(env.db)
        await runs.execute_room_run(
            env.db.tenant, room.id, trigger="manual", requested_by=env.db.alice, preview=True
        )
        assert env.runner.calls[0]["agent_config"].hard_budget is True

    async def test_a_preview_needs_a_requester(self, env):
        room = _report(env.db)
        with pytest.raises(ValueError):
            await runs.execute_room_run(env.db.tenant, room.id, trigger="manual", preview=True)


# ─── B6 / A5: run now ─────────────────────────────────────────────────────


class TestRunNow:
    async def test_a_shared_member_runs_it_now(self, env):
        room = _report(env.db)

        edition, joined = await runs.start_room_run(_actor(env.db, "bob"), room.id)
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)

        assert joined is False and edition.status == "building"
        assert edition.requested_by == env.db.bob
        done = dal.get_edition(env.db.tenant, edition.id, viewer_id=None)
        assert done.status == "published"
        # Run as the run-as user, not as whoever pressed the button.
        assert env.runner.calls[0]["user_id"] == env.db.alice
        assert env.runner.calls[0]["trigger_type"] == TriggerType.MANUAL

    async def test_a_viewer_is_refused(self, env):
        room = _report(env.db)
        with pytest.raises(RoomForbidden):
            await runs.start_room_run(_actor(env.db, "vera"), room.id)
        assert env.runner.calls == []

    async def test_a_run_in_progress_is_joined(self, env):
        room = _report(env.db)
        env.runner.gate = asyncio.Event()

        first, joined_first = await runs.start_room_run(_actor(env.db, "alice"), room.id)
        await asyncio.wait_for(env.runner.entered.wait(), 10)
        second, joined_second = await runs.start_room_run(_actor(env.db, "bob"), room.id)
        env.runner.gate.set()
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)

        assert (joined_first, joined_second) == (False, True)
        assert second.id == first.id
        assert len(env.runner.calls) == 1

    async def test_run_now_works_while_paused_for_an_assigned_user(self, env):
        room = _report(env.db)
        dal.set_paused(env.db.tenant, room.id, True)

        edition, _ = await runs.start_room_run(_actor(env.db, "alice"), room.id)
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)

        assert dal.get_edition(env.db.tenant, edition.id, viewer_id=None).status == "published"

    async def test_a_shared_member_cannot_run_a_paused_room(self, env):
        room = _report(env.db)
        dal.set_paused(env.db.tenant, room.id, True)

        with pytest.raises(runs.RunRefused) as refused:
            await runs.start_room_run(_actor(env.db, "bob"), room.id)
        assert refused.value.reason == "paused"

    async def test_an_archived_room_is_refused(self, env):
        room = _report(env.db)
        dal.archive_room(env.db.tenant, room.id)
        with pytest.raises(runs.RunRefused) as refused:
            await runs.start_room_run(_actor(env.db, "alice"), room.id)
        assert refused.value.reason == "archived"

    async def test_the_daily_cap_counts_previews_and_runs(self, env):
        room = _report(env.db)
        dal.update_definition(
            env.db.tenant,
            room.id,
            {"settings": {"max_on_demand_per_day": 2}},
            env.db.alice,
            room.rev,
        )
        await runs.start_room_run(_actor(env.db, "alice"), room.id, preview=True)
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)
        await runs.start_room_run(_actor(env.db, "bob"), room.id)
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)

        with pytest.raises(runs.OnDemandCapReached) as capped:
            await runs.start_room_run(_actor(env.db, "alice"), room.id, preview=True)
        assert (capped.value.limit, capped.value.used) == (2, 2)

    async def test_a_named_period_is_built(self, env):
        room = _report(env.db)
        period = {"grain": "week", "start": "2026-11-09", "end": "2026-11-15"}

        edition, _ = await runs.start_room_run(_actor(env.db, "alice"), room.id, period=period)
        await asyncio.wait_for(asyncio.gather(*runs.pending_runs()), 10)

        assert (edition.grain, edition.period_start, edition.period_end) == (
            "week",
            date(2026, 11, 9),
            date(2026, 11, 15),
        )

    async def test_resume_computes_the_next_run_from_now(self, env):
        room = _report(env.db, schedule_text="every 1 hour")
        alice = _actor(env.db, "alice")
        definition.apply_definition_change(alice, room.id, room.rev, {"paused": True})
        env.db.cur.execute(
            "UPDATE user_cronjobs SET next_run_at = now() - interval '3 days' "
            "WHERE tenant_id = %s AND room_id = %s",
            (env.db.tenant, room.id),
        )
        current = dal.get_room(env.db.tenant, room.id)

        definition.apply_definition_change(alice, room.id, current.rev, {"paused": False})

        nxt = datetime.fromisoformat(
            definition.room_schedule(env.db.tenant, room.id)["next_run_at"]
        )
        assert nxt > datetime.now(UTC) + timedelta(minutes=50)


# ─── the period entry point lane 8 calls ──────────────────────────────────


class TestPeriodRun:
    async def test_run_room_completes_the_edition_it_is_given(self, env):
        room = _report(env.db)
        period = runs.Period(date(2026, 11, 9), date(2026, 11, 15), "week")
        building, _ = dal.create_edition(
            env.db.tenant,
            room.id,
            grain="week",
            period_start=period.start,
            period_end=period.end,
            requested_by=env.db.bob,
        )

        done = await runs.run_room(room, building, period=period, context="Day one: up 2%.")

        assert done.id == building.id and done.status == "published"
        assert env.runner.carriers[0].edition_id == building.id
        assert "Day one: up 2%." in env.runner.calls[0]["message"]
        assert env.runner.calls[0]["user_id"] == env.db.alice
        # An on-demand period run tells its requester only.
        assert env.fanouts == [(building.id, {"recipients": [env.db.bob], "emit_event": False})]

    async def test_run_room_raises_when_it_cannot_run(self, env):
        room = _report(env.db)
        dal.archive_room(env.db.tenant, room.id)
        period = runs.Period(date(2026, 11, 9), date(2026, 11, 15), "week")
        building, _ = dal.create_edition(
            env.db.tenant,
            room.id,
            grain="week",
            period_start=period.start,
            period_end=period.end,
            requested_by=env.db.bob,
        )
        with pytest.raises(runs.RunRefused):
            await runs.run_room(room, building, period=period, context="")
