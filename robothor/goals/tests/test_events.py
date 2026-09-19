import json
from datetime import UTC, datetime
from unittest.mock import patch

from robothor.goals import store
from robothor.goals.events import capture
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.tests.test_store import db, private_database  # noqa: F401


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
