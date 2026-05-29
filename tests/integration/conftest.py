"""Local conftest for tests/integration/test_task_lifecycle.py.

Reuses the repo-wide `db_conn` / `db_cursor` / `mock_get_connection`
fixtures from ``tests/conftest_integration.py`` and adds task-system
specific helpers: an autouse cleanup that wipes any rows whose title
starts with the per-test ``test_prefix`` — plus their spawned subtasks —
so a failure in one test can't leak data into the next.
"""

from __future__ import annotations

import pytest

# Anything in this directory is integration by default.
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_task_state(db_conn, test_prefix):
    """Wipe prefixed task rows, their spawned subtasks, and all their history.

    Runs before and after each test so a half-complete fixture build can't
    interfere with the next test. Uses LIKE on title — the ``test_prefix``
    fixture from the repo-root ``conftest.py`` is a 16-char hex string that
    never collides with production data.

    Subtasks created by reject/promotion (``reject_task``,
    ``promote_todo_to_subtask``) take titles from change-requests / item
    content and therefore don't carry the prefix, so they're wiped via their
    ``parent_task_id`` instead. One level of children covers these tests; they
    don't spawn grandchildren.
    """
    cur = db_conn.cursor()
    pattern = f"{test_prefix}%"

    def _purge():
        try:
            # Children first (their parent_task_id points at the prefixed rows).
            cur.execute(
                "DELETE FROM crm_task_history WHERE task_id IN "
                "(SELECT id FROM crm_tasks WHERE parent_task_id IN "
                "(SELECT id FROM crm_tasks WHERE title LIKE %s))",
                (pattern,),
            )
            cur.execute(
                "DELETE FROM crm_tasks WHERE parent_task_id IN "
                "(SELECT id FROM crm_tasks WHERE title LIKE %s)",
                (pattern,),
            )
            # Then the prefixed rows themselves.
            cur.execute(
                "DELETE FROM crm_task_history WHERE task_id IN "
                "(SELECT id FROM crm_tasks WHERE title LIKE %s)",
                (pattern,),
            )
            cur.execute("DELETE FROM crm_tasks WHERE title LIKE %s", (pattern,))
            db_conn.commit()
        except Exception:
            db_conn.rollback()

    _purge()
    yield
    try:
        _purge()
    finally:
        cur.close()
