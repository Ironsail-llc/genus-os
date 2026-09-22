"""Reports follow persisted goal facts without claiming tasks prove completion."""

import pytest

from robothor.engine.tests.runtime_fixtures import seed_unfinished_work
from robothor.goals import store
from robothor.goals.presentation import render_goal_progress
from robothor.goals.tests.test_store import change, create, db, private_database  # noqa: F401


def test_unfinished_report_and_pause_follow_stored_state(db):  # noqa: F811
    store.set_enabled(db, False, "operator")
    seed_unfinished_work(db)
    (listed,) = store.list_goals(db)
    goal = store.get(db, listed["id"])
    text = render_goal_progress(goal, execution_enabled=store.enabled(db))
    assert "1 of 2 linked tasks are marked done" in text
    assert "Check the remaining item" in text
    assert "The goal is not complete" in text
    assert "No current criterion evidence" in text
    assert "scheduled reviews will not run" in text
    assert goal["ready_at"] not in text
    assert goal["id"] not in text
    change(db, goal, "pause")
    store.set_enabled(db, True, "operator")
    paused = store.get(db, goal["id"])
    text = render_goal_progress(paused, execution_enabled=store.enabled(db))
    assert "is paused" in text and "will not run while the goal is paused" in text
    assert "Check the remaining item" in text
    assert sorted(task["status"] for task in paused["tasks"]) == ["DONE", "TODO"]
    assert store.claim(db) is None


def test_all_tasks_done_still_does_not_complete_goal_and_missing_tasks_fail(db):  # noqa: F811
    seed_unfinished_work(db)
    (listed,) = store.list_goals(db)
    with store.transaction() as cur:
        cur.execute("UPDATE crm_tasks SET status='DONE' WHERE tenant_id=%s", (db,))
    goal = store.get(db, listed["id"])
    text = render_goal_progress(goal, execution_enabled=True)
    assert "2 of 2 linked tasks are marked done" in text
    assert "The goal is not complete" in text
    assert "No current criterion evidence" in text
    assert "Still open" not in text
    assert "A review is registered" in text
    with pytest.raises(KeyError, match="tasks"):
        render_goal_progress(listed, execution_enabled=True)


def test_ongoing_goal_is_not_reported_as_finite_completion(db):  # noqa: F811
    goal = create(db, mode="ongoing", kind="long")
    text = render_goal_progress(store.get(db, goal["id"]), execution_enabled=False)
    assert "ongoing goal; completed tasks do not finish it" in text
    assert "No tasks are linked" in text


def test_paused_child_work_and_pending_recovery_remain_visible(db):  # noqa: F811
    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    change(db, parent, "pause")
    # Represent an interrupted child whose external results still need reconciliation.
    with store.transaction() as cur:
        child_state = store.locked(cur, db, child["id"])
        child_state["recovery_required"] = True
        store.save(cur, db, child_state)
    goal = store.get(db, parent["id"])
    text = render_goal_progress(goal, execution_enabled=True)
    assert "Child work remains unfinished" in text and "(paused)" in text
    assert "earlier actions still need verification" in text
    assert "The goal is not complete" in text
    assert "No tasks are linked" in text


def test_canceled_and_unknown_tasks_are_not_reported_as_done(db):  # noqa: F811
    seed_unfinished_work(db)
    (listed,) = store.list_goals(db)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE crm_tasks SET status=CASE WHEN status='DONE' THEN 'CANCELED' ELSE NULL END WHERE tenant_id=%s",
            (db,),
        )
    text = render_goal_progress(store.get(db, listed["id"]), execution_enabled=False)
    assert "0 of 2 linked tasks are marked done" in text
    assert "1 linked task is canceled" in text
    assert "1 linked task has an unrecognized status" in text
    assert "Still open" not in text


def test_large_task_list_reports_omissions_and_bounds_titles(db):  # noqa: F811
    from uuid import uuid4

    goal = create(db)
    with store.transaction() as cur:
        for _ in range(12):
            task_id = str(uuid4())
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,%s,'TODO')",
                (task_id, db, "Long task title " * 100),
            )
            cur.execute(
                "INSERT INTO pursuit_goal_tasks(tenant_id,goal_id,task_id) VALUES (%s,%s,%s)",
                (db, goal["id"], task_id),
            )
    text = render_goal_progress(store.get(db, goal["id"]), execution_enabled=False)
    assert "0 of 12 linked tasks" in text
    assert "and 9 more" in text
    assert len(text) < 1000 and "…" in text
