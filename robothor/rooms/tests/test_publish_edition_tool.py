"""``room_publish_edition`` end to end against real Postgres.

Stories: B2 (blocks validated before anything is stored), B3 (provenance from
the recorded run; anything provenance-like in the arguments ignored), D4 (an
ad-hoc dashboard in any kind of room), a preview edition is never listed for
another user, and the saved assistant message carries the edition id.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.rooms import dal, editions
from robothor.rooms.run_context import RoomRun, room_run_scope
from robothor.rooms.tests.conftest import committed_connection

pytestmark = pytest.mark.integration

WEEK = {"grain": "week", "start": "2026-11-09", "end": "2026-11-15"}
BLOCKS = [
    {"component": "render_metric_card", "props": {"title": "Revenue", "value": 1200}},
    {"component": "render_markdown", "props": {"content": "Up 4% on last week."}},
]


@pytest.fixture
def env(rooms_db, monkeypatch) -> Any:
    """``rooms_db`` plus a recorded run, a captured event sink and hook log."""
    from robothor.engine import chat_store

    monkeypatch.setattr(editions, "get_connection", committed_connection)
    monkeypatch.setattr(chat_store, "get_connection", committed_connection)
    events: list[tuple[Any, Any]] = []
    hooks: list[tuple[Any, Any]] = []
    monkeypatch.setattr(editions, "emit_edition_event", lambda r, e: events.append((r, e)))

    def hook(room: Any, edition: Any) -> None:
        hooks.append((room, edition))

    editions.register_edition_published(hook)
    run_id = str(uuid.uuid4())
    started = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
    rooms_db.cur.execute(
        "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, status, started_at, "
        "user_id) VALUES (%s, %s, 'main', 'cron', 'running', %s, %s)",
        (run_id, rooms_db.tenant, started, rooms_db.alice),
    )
    for n, (tool, inner, err) in enumerate(
        [
            ("crm_search", None, None),
            ("tool_call", "gws_gmail_search", None),
            ("web_fetch", None, "HTTP 500"),
        ],
        start=1,
    ):
        rooms_db.cur.execute(
            "INSERT INTO agent_run_steps (run_id, step_number, step_type, tool_name, tool_input, "
            "started_at, error_message) VALUES (%s, %s, 'tool_call', %s, %s, %s, %s)",
            (
                run_id,
                n,
                tool,
                f'{{"name": "{inner}"}}' if inner else "{}",
                started,
                err,
            ),
        )
    try:
        yield SimpleNamespace(
            db=rooms_db, run_id=run_id, started=started, events=events, hooks=hooks
        )
    finally:
        editions.unregister_edition_published(hook)
        rooms_db.cur.execute("DELETE FROM agent_runs WHERE id = %s", (run_id,))


def room(env: Any, kind: str = "report") -> Any:
    db = env.db
    r = dal.create_room(
        db.tenant,
        kind=kind,
        title="Weekly numbers",
        agent_id="main",
        created_by=db.alice,
        settings={"tz": "UTC", "default_grain": "week"},
    )
    dal.add_member(db.tenant, r.id, db.bob, added_by=db.alice)
    return r


def ctx(env: Any, user: str | None = None, role: str = "member", agent: str = "main") -> Any:
    return ToolContext(
        agent_id=agent,
        run_id=env.run_id,
        tenant_id=env.db.tenant,
        user_id=user or env.db.alice,
        user_role=role,
    )


async def publish(args: dict[str, Any], context: Any) -> dict[str, Any]:
    from robothor.engine.tools.handlers.room_editions import HANDLERS

    return await HANDLERS["room_publish_edition"](args, context)


def carrier(r: Any, **kw: Any) -> RoomRun:
    return RoomRun(tenant_id=r.tenant_id, room_id=r.id, session_key=r.session_key, **kw)


def args(r: Any, **kw: Any) -> dict[str, Any]:
    return {"room_id": r.id, "title": "Week 46", "summary": "Up 4%.", "blocks": BLOCKS} | kw


def stored(env: Any, r: Any) -> list[Any]:
    return dal.list_editions(env.db.tenant, r.id, viewer_id=None, include_unpublished=True)


# ─── who may publish, from where ─────────────────────────────────────────


async def test_the_tool_is_registered_with_its_schema() -> None:
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    schema = get_engine_schemas()["room_publish_edition"]["function"]["parameters"]
    assert {"room_id", "title", "summary", "blocks"} <= set(schema["required"])
    assert "provenance" not in schema["properties"]
    assert "preview" not in schema["properties"]
    assert "room_publish_edition" in builtin_handlers()


async def test_outside_a_room_run_nothing_is_published(env) -> None:
    r = room(env)
    result = await publish(args(r, period=WEEK), ctx(env))
    assert "error" in result and "workspace" in result["error"]
    assert stored(env, r) == []


async def test_a_run_in_another_room_cannot_publish_here(env) -> None:
    here, elsewhere = room(env), room(env)
    with room_run_scope(carrier(elsewhere)):
        result = await publish(args(here, period=WEEK), ctx(env))
    assert "error" in result
    assert stored(env, here) == []


async def test_another_agent_inheriting_the_run_cannot_publish(env) -> None:
    r = room(env)
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env, agent="helper"))
    assert "error" in result
    assert stored(env, r) == []


@pytest.mark.parametrize(("who", "role"), [("carol", "member"), ("bob", "viewer")])
async def test_a_caller_without_post_access_cannot_publish(env, who, role) -> None:
    r = room(env)
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env, getattr(env.db, who), role))
    assert "error" in result
    assert stored(env, r) == []


# ─── B2: content validated before anything is stored ─────────────────────


async def test_a_bad_block_is_refused_by_index_and_nothing_is_stored(env) -> None:
    r = room(env)
    bad = [*BLOCKS, {"component": "render_iframe", "props": {"src": "https://example.com"}}]
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK, blocks=bad), ctx(env))
    assert "block 2" in result["error"]
    assert stored(env, r) == []
    assert env.events == [] and env.hooks == []


# ─── editions and periods ────────────────────────────────────────────────


async def test_an_edition_for_a_period_is_published(env) -> None:
    r = room(env)
    with room_run_scope(carrier(r)):
        result = await publish(
            args(
                r,
                period=WEEK,
                summary="Up 4%\x1b[31m.",
                suggestions=[{"key": "k", "title": "Call Bob"}],
            ),
            ctx(env),
        )
    assert result["edition_id"] and result["link"] == f"/?v=chat&room={r.id}"
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert (e.status, e.kind, e.preview, e.seq, e.revision) == ("published", "edition", False, 1, 1)
    assert (e.grain, e.period_start, e.period_end) == (
        "week",
        date(2026, 11, 9),
        date(2026, 11, 15),
    )
    assert e.summary == "Up 4%."
    assert e.blocks == BLOCKS
    assert e.suggestions == [{"key": "k", "title": "Call Bob", "detail": "", "action_hint": ""}]
    assert e.run_id == env.run_id and e.requested_by == env.db.alice


async def test_the_period_defaults_from_the_run(env) -> None:
    r = room(env)
    run = carrier(r, grain="day", period_start=date(2026, 11, 20), period_end=date(2026, 11, 20))
    with room_run_scope(run):
        result = await publish(args(r), ctx(env))
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert (e.grain, e.period_start, e.period_end) == (
        "day",
        date(2026, 11, 20),
        date(2026, 11, 20),
    )


async def test_an_edition_needs_a_period_and_a_report_room(env) -> None:
    report, agent = room(env), room(env, kind="agent")
    with room_run_scope(carrier(report)):
        no_period = await publish(args(report), ctx(env))
    with room_run_scope(carrier(agent)):
        wrong_kind = await publish(args(agent, period=WEEK), ctx(env))
    assert "period" in no_period["error"]
    assert "report" in wrong_kind["error"]
    assert stored(env, report) == [] and stored(env, agent) == []


async def test_publishing_completes_the_building_row_for_that_period(env) -> None:
    r = room(env)
    building, _ = dal.create_edition(
        env.db.tenant,
        r.id,
        grain="week",
        period_start=date(2026, 11, 9),
        period_end=date(2026, 11, 15),
        requested_by=env.db.bob,
    )
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env))
    assert result["edition_id"] == building.id
    assert [e.status for e in stored(env, r)] == ["published"]


async def test_the_schedulers_building_row_is_the_one_completed(env) -> None:
    r = room(env)
    building, _ = dal.create_edition(
        env.db.tenant,
        r.id,
        grain="week",
        period_start=date(2026, 11, 9),
        period_end=date(2026, 11, 15),
        requested_by=env.db.alice,
        run_id=env.run_id,
    )
    with room_run_scope(carrier(r, edition_id=building.id)):
        result = await publish(args(r), ctx(env))
        other = await publish(args(r, period={**WEEK, "start": "2026-11-02"}), ctx(env))
    assert result["edition_id"] == building.id
    assert "period" in other["error"]


async def test_republishing_a_period_increments_its_revision(env) -> None:
    r = room(env)
    with room_run_scope(carrier(r)):
        first = await publish(args(r, period=WEEK), ctx(env))
        second = await publish(args(r, period=WEEK), ctx(env))
        other_week = await publish(
            args(r, period={**WEEK, "start": "2026-11-02", "end": "2026-11-08"}), ctx(env)
        )
    revisions = {
        x["edition_id"]: dal.get_edition(env.db.tenant, x["edition_id"], viewer_id=None).revision
        for x in (first, second, other_week)
    }
    assert [revisions[x["edition_id"]] for x in (first, second, other_week)] == [1, 2, 1]
    assert second["revision"] == 2


# ─── D4: an ad-hoc dashboard in any room ─────────────────────────────────


@pytest.mark.parametrize("kind", ["report", "agent", "task"])
async def test_a_dashboard_can_be_published_in_any_room(env, kind) -> None:
    if kind == "task":
        env.db.cur.execute(
            "INSERT INTO crm_tasks (tenant_id, title) VALUES (%s, 'Call Bob') RETURNING id",
            (env.db.tenant,),
        )
        r = dal.get_or_create_task_room(
            env.db.tenant, str(env.db.cur.fetchone()[0]), agent_id="main", created_by=env.db.alice
        )
    else:
        r = room(env, kind=kind)
    with room_run_scope(carrier(r)):
        result = await publish(args(r, kind="dashboard"), ctx(env))
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert (e.kind, e.status, e.period_start, e.grain) == ("dashboard", "published", None, None)


async def test_a_dashboard_takes_no_period(env) -> None:
    r = room(env, kind="agent")
    with room_run_scope(carrier(r)):
        result = await publish(args(r, kind="dashboard", period=WEEK), ctx(env))
    assert "period" in result["error"]


# ─── B3: provenance from the recorded run ────────────────────────────────


async def test_provenance_comes_from_the_recorded_run_and_forgery_is_ignored(env) -> None:
    r = room(env)
    forged = {"run_id": "run-forged", "ran_as": env.db.carol, "tool_calls": [{"tool": "x"}]}
    with room_run_scope(carrier(r)):
        result = await publish(
            args(r, period=WEEK, provenance=forged, ran_as=env.db.carol, preview=True),
            ctx(env),
        )
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert e.preview is False  # preview is the run's, never an argument
    assert e.provenance["run_id"] == env.run_id
    assert e.provenance["ran_as"] == env.db.alice
    assert e.provenance["started_at"] == env.started.isoformat()
    assert e.provenance["room_rev"] == 1
    assert [c["tool"] for c in e.provenance["tool_calls"]] == [
        "crm_search",
        "gws_gmail_search",
        "web_fetch",
    ]
    assert [c["ok"] for c in e.provenance["tool_calls"]] == [True, True, False]


# ─── what a publish sets off ─────────────────────────────────────────────


async def test_a_publish_emits_the_event_runs_the_hooks_and_bumps_activity(env) -> None:
    r = room(env)
    before = dal.get_room(env.db.tenant, r.id).last_activity_at
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env))
    assert [(x.id, y.id) for x, y in env.events] == [(r.id, result["edition_id"])]
    assert [(x.id, y.id) for x, y in env.hooks] == [(r.id, result["edition_id"])]
    assert dal.get_room(env.db.tenant, r.id).last_activity_at > before


def test_the_event_payload_names_the_room_and_never_the_session_key(env) -> None:
    r = room(env)
    e, _ = dal.create_edition(env.db.tenant, r.id, kind="dashboard")
    payload = editions.edition_event_payload(r, e)
    assert payload["room_id"] == r.id and payload["tenant_id"] == env.db.tenant
    assert payload["edition_id"] == e.id
    assert r.session_key not in str(payload)


async def test_a_preview_is_for_its_requester_only_and_sets_nothing_off(env) -> None:
    r = room(env)
    with room_run_scope(carrier(r, preview=True, requested_by=env.db.bob)):
        result = await publish(args(r, period=WEEK), ctx(env))
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=env.db.bob)
    assert e.preview and e.requested_by == env.db.bob and result["preview"] is True
    assert dal.list_editions(env.db.tenant, r.id, viewer_id=env.db.alice) == []
    assert dal.get_edition(env.db.tenant, e.id, viewer_id=env.db.alice) is None
    assert env.events == [] and env.hooks == []
    assert editions.take_turn_editions(env.db.tenant, r.session_key) == []


# ─── the saved assistant message carries the edition ─────────────────────


async def test_the_assistant_message_saved_for_the_turn_carries_the_edition(env) -> None:
    from robothor.engine import chat_store

    r = room(env)
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env))
    ids = chat_store.save_exchange(
        r.session_key, "How was the week?", "Here it is.", channel="webchat", tenant_id=r.tenant_id
    )
    later = chat_store.save_exchange(
        r.session_key, "Thanks", "You're welcome.", channel="webchat", tenant_id=r.tenant_id
    )
    env.db.cur.execute(
        "SELECT id, message FROM chat_messages WHERE id = ANY(%s) ORDER BY id", (ids + later,)
    )
    payloads = {row[0]: row[1] for row in env.db.cur.fetchall()}
    assert payloads[ids[1]]["edition_id"] == result["edition_id"]
    assert "edition_id" not in payloads[ids[0]]
    assert "edition_id" not in payloads[later[1]]
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert e.chat_message_id == ids[1]


async def test_an_assistant_message_saved_alone_carries_the_edition(env) -> None:
    from robothor.engine import chat_store

    r = room(env)
    with room_run_scope(carrier(r)):
        result = await publish(args(r, period=WEEK), ctx(env))
    message_id = chat_store.save_message(
        r.session_key, "assistant", "This week's numbers.", channel="webchat", tenant_id=r.tenant_id
    )
    e = dal.get_edition(env.db.tenant, result["edition_id"], viewer_id=None)
    assert e.chat_message_id == message_id


# ─── off the loop ────────────────────────────────────────────────────────


async def test_the_publish_runs_off_the_event_loop(env, monkeypatch) -> None:
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor

    await warm_executor()
    r = room(env)
    probe = SlowProbe(asyncio.get_running_loop(), result={"edition_id": "e"})
    monkeypatch.setattr(editions, "publish_from_run", probe)
    with room_run_scope(carrier(r)):
        await publish(args(r, period=WEEK), ctx(env))
    await probe.settle()
    probe.assert_loop_free()
