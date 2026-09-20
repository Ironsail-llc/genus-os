import json
from datetime import UTC, datetime
from unittest.mock import patch

from robothor.goals import store
from robothor.goals.events import capture
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.tests.test_store import db, private_database  # noqa: F401


def stream_of(events):
    class Redis:
        def xrange(self, key, **kwargs):
            return [] if kwargs["min"] == f"({events[-1][0]}" else events

    return Redis()


def envelope(tenant, kind, payload, timestamp=None):
    return {
        "tenant_id": tenant,
        "type": kind,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "payload": json.dumps(payload),
    }


def test_capture_stores_only_what_some_goal_waits_on(db):  # noqa: F811
    """All eight bus streams used to be copied into PostgreSQL every sixty
    seconds whether or not anything could ever match them."""
    g = store.create(
        db,
        CreateGoal(objective="Get reply", success_criteria=["Reply received"], kind="long"),
        "operator",
    )
    events = [
        ("1-0", envelope(db, "email.new", {"thread_id": "abc"})),
        ("2-0", envelope(db, "calendar.changed", {"event_id": "xyz"})),
    ]
    redis = stream_of(events)
    with (
        patch("robothor.events.bus._get_redis", return_value=redis),
        patch("robothor.events.bus.VALID_STREAMS", {"email"}),
    ):
        # Nothing is waiting, so nothing is worth storing.
        capture(db)
        with store.transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM pursuit_goal_events WHERE tenant_id=%s", (db,))
            assert cur.fetchone()["n"] == 0
            # The cursor still advanced: a watch registered now starts from
            # now, which is the existing contract.
            cur.execute("SELECT cursors FROM goal_pursuit_settings WHERE tenant_id=%s", (db,))
            assert cur.fetchone()["cursors"] == {"email": "2-0"}

        store.update(
            db,
            g["id"],
            GoalUpdate(
                action="wait",
                version=g["version"],
                note="Wait for thread",
                event_type="email.new",
                event_match={"thread_id": "abc"},
            ),
            "main",
        )
        with store.transaction() as cur:
            cur.execute(
                "UPDATE goal_pursuit_settings SET cursors='{}'::jsonb WHERE tenant_id=%s", (db,)
            )
        capture(db)
    with store.transaction() as cur:
        cur.execute("SELECT event_type FROM pursuit_goal_events WHERE tenant_id=%s", (db,))
        assert [r["event_type"] for r in cur.fetchall()] == ["email.new"]


def test_processed_events_and_aged_history_are_pruned(db):  # noqa: F811
    """Neither table had any retention at all."""
    store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    store.ingest_event(db, "old", "email.new", {})
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goal_events SET processed_at=now()-interval '30 days' WHERE tenant_id=%s",
            (db,),
        )
        cur.execute(
            "UPDATE pursuit_goal_history SET created_at=now()-interval '365 days' WHERE tenant_id=%s",
            (db,),
        )
    store.ingest_event(db, "fresh", "email.new", {})
    store.prune(db)
    with store.transaction() as cur:
        cur.execute("SELECT id FROM pursuit_goal_events WHERE tenant_id=%s", (db,))
        assert [r["id"] for r in cur.fetchall()] == ["fresh"]
        cur.execute("SELECT count(*) AS n FROM pursuit_goal_history WHERE tenant_id=%s", (db,))
        assert cur.fetchone()["n"] == 0


def test_replayed_events_commit_with_cursor_and_ignore_foreign_or_malformed(db):  # noqa: F811
    g = store.create(
        db,
        CreateGoal(objective="Get reply", success_criteria=["Reply received"], kind="long"),
        "operator",
    )
    store.update(
        db,
        g["id"],
        GoalUpdate(
            action="wait",
            version=1,
            note="Wait for thread",
            event_type="email.new",
            event_match={"thread_id": "abc"},
        ),
        "main",
    )

    def envelope(tenant, payload, timestamp=None):
        return {
            "tenant_id": tenant,
            "type": "email.new",
            "timestamp": timestamp or datetime.now(UTC).isoformat(),
            "payload": json.dumps(payload),
        }

    events = [
        ("1-0", envelope("foreign", {"thread_id": "abc"})),
        ("2-0", envelope(db, [])),
        ("3-0", envelope(db, {}, "invalid timestamp")),
        ("4-0", envelope(db, {"thread_id": "abc"})),
    ]

    class Redis:
        def xrange(self, key, **kwargs):
            return [] if kwargs["min"] == "(4-0" else events

    with (
        patch("robothor.events.bus._get_redis", return_value=Redis()),
        patch("robothor.events.bus.VALID_STREAMS", {"email"}),
    ):
        capture(db)
        capture(db)
    with store.transaction() as cur:
        cur.execute("SELECT cursors FROM goal_pursuit_settings WHERE tenant_id=%s", (db,))
        assert cur.fetchone()["cursors"] == {"email": "4-0"}
        cur.execute("SELECT count(*) AS n FROM pursuit_goal_events WHERE tenant_id=%s", (db,))
        assert cur.fetchone()["n"] == 1
    assert store.claim(db)[0]["id"] == g["id"]
