"""Goal discovery must expose complete counts without unbounded task payloads."""

from uuid import uuid4

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.goals import store
from robothor.goals.tests.test_store import (  # noqa: F401
    change,
    create,
    db,
    private_database,
)
from robothor.goals.tools import HANDLERS


@pytest.mark.asyncio
async def test_listing_has_bounded_fresh_task_facts_without_claiming_completion(db):  # noqa: F811
    goal = create(db)
    empty = create(db)
    other = create(str(uuid4()))
    task_ids = []
    for index, status in enumerate(["DONE", "TODO", "TODO", "TODO", "TODO", "TODO"]):
        task_id = str(uuid4())
        task_ids.append(task_id)
        with store.transaction() as cur:
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,%s,%s)",
                (task_id, db, f"Task {index}", status),
            )
        goal = change(db, goal, "link_task", task_id=task_id)
    ctx = ToolContext(agent_id="main", user_role="owner", tenant_id=db)
    listing = await HANDLERS["list_pursuit_goals"]({}, ctx)
    by_id = {g["id"]: g for g in listing["goals"]}
    assert set(by_id) == {goal["id"], empty["id"]}
    assert other["id"] not in by_id
    summary = by_id[goal["id"]]["task_summary"]
    assert summary["total"] == 6
    assert summary["by_status"]["DONE"] == {
        "count": 1,
        "preview": [{"id": task_ids[0], "title": "Task 0"}],
        "truncated": False,
    }
    todo = summary["by_status"]["TODO"]
    assert todo["count"] == 5 and todo["truncated"] is True
    assert len(todo["preview"]) == 3
    assert {task["id"] for task in todo["preview"]} <= set(task_ids[1:])
    assert by_id[empty["id"]]["task_summary"] == {"total": 0, "by_status": {}}
    assert all("task_summary" not in g for g in store.list_goals(db))
    # A second read reflects task changes without changing the goal's evidence or version.
    with store.transaction() as cur:
        cur.execute("UPDATE crm_tasks SET status='DONE' WHERE tenant_id=%s", (db,))
    listing = await HANDLERS["list_pursuit_goals"]({}, ctx)
    updated = next(g for g in listing["goals"] if g["id"] == goal["id"])
    assert updated["task_summary"]["by_status"]["DONE"]["count"] == 6
    assert "TODO" not in updated["task_summary"]["by_status"]
    assert updated["status"] == goal["status"] != "complete"
    assert updated["version"] == goal["version"]
    assert store.get(db, goal["id"])["version"] == goal["version"]
