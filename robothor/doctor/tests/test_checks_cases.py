"""Cases past due that nobody owns, or that wait on a person, are reported.

A case is a CRM task (``docs/CRM_DATA_FABRIC.md``, "Cases"). The check asks the
database for open tasks past ``due_at`` that either have no assigned agent or
are flagged ``requires_human`` with the question still unanswered, and warns
with a count per kind and the oldest task id of each. An overdue task an agent
owns is that agent's to finish; it is not counted.
"""

from __future__ import annotations

import asyncio

from robothor.doctor import registry
from robothor.doctor.tests.conftest import fake_db, make_ctx

UNOWNED_ID = "11111111-1111-4111-8111-111111111111"
NEEDS_PERSON_ID = "22222222-2222-4222-8222-222222222222"
TENANT = "test-tenant"
OTHER_TENANT = "other-tenant"


def _check():
    return next(c for c in registry.builtin_checks() if c.id == "crm.cases_overdue")


def _run(ctx):
    return asyncio.run(_check().run(ctx))


def test_the_check_is_registered_as_a_recommended_crm_check() -> None:
    check = _check()
    assert check.category == "crm"
    assert check.severity == "recommended"


def test_cases_overdue_warns_on_an_overdue_unowned_task() -> None:
    # One row per kind: (needs_person, count, oldest id, oldest tenant).
    db = fake_db([(False, 1, UNOWNED_ID, TENANT)])
    result = _run(make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "1 unowned" in result.detail
    assert UNOWNED_ID in result.detail


def test_the_query_counts_only_open_overdue_tasks_without_an_owner_or_waiting_on_a_person() -> None:
    db = fake_db([(False, 1, UNOWNED_ID, TENANT)])
    _run(make_ctx(db_factory=db))
    sql = " ".join(db.connection.cursors[0].executed[0][0].split())
    assert "FROM crm_tasks" in sql
    assert "deleted_at IS NULL" in sql
    assert "status IS DISTINCT FROM 'DONE'" in sql
    assert "due_at < NOW()" in sql
    # An overdue task an agent owns, with nobody asked anything, is not counted.
    assert "(assigned_to_agent IS NULL OR requires_human IS TRUE)" in sql


def test_a_task_waiting_on_a_person_is_counted_as_needing_a_person() -> None:
    db = fake_db([(True, 1, NEEDS_PERSON_ID, TENANT)])
    result = _run(make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "1 needs a person" in result.detail
    assert NEEDS_PERSON_ID in result.detail


def test_both_kinds_are_reported_with_their_counts_and_oldest_ids() -> None:
    db = fake_db([(False, 3, UNOWNED_ID, TENANT), (True, 2, NEEDS_PERSON_ID, OTHER_TENANT)])
    result = _run(make_ctx(db_factory=db))
    assert result.status == "fail"
    assert "5 overdue cases" in result.detail
    assert "3 unowned" in result.detail
    assert "2 needs a person" in result.detail
    assert UNOWNED_ID in result.detail
    assert NEEDS_PERSON_ID in result.detail


def test_the_oldest_task_is_named_with_its_tenant() -> None:
    db = fake_db([(False, 3, UNOWNED_ID, TENANT), (True, 2, NEEDS_PERSON_ID, OTHER_TENANT)])
    result = _run(make_ctx(db_factory=db))
    assert f"{UNOWNED_ID} in tenant {TENANT}" in result.detail
    assert f"{NEEDS_PERSON_ID} in tenant {OTHER_TENANT}" in result.detail


def test_a_task_snoozed_until_a_future_follow_up_is_not_overdue() -> None:
    # Same rule as list_tasks: a future follow_up_at parks the task.
    db = fake_db([])
    _run(make_ctx(db_factory=db))
    sql = " ".join(db.connection.cursors[0].executed[0][0].split())
    assert "(follow_up_at IS NULL OR follow_up_at <= NOW())" in sql


def test_no_overdue_cases_is_ok() -> None:
    result = _run(make_ctx(db_factory=fake_db([])))
    assert result.status == "pass"


def test_an_unreachable_database_is_a_skip_not_a_crash() -> None:
    result = _run(make_ctx(db_factory=fake_db(error=RuntimeError("no database"))))
    assert result.status == "skip"
