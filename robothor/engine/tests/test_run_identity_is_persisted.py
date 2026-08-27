"""Every agent run must record who caused it.

`agent_runs` has `user_id`, `user_role` and `tenant_id`, and `AgentRun`
carries all three. `create_run` inserted only `tenant_id`, and `update_run`
had no field for the other two — so the columns defaulted to '' and stayed
there. On this box, all 6,144 runs in the last 30 days carry an empty
`user_role`. The audit trail exists as a schema and holds nothing.

That is load-bearing for federation. The whole point of Phase 1 was that an
inbound op runs as a real principal instead of the allow-all `service` role —
but if the run row does not record the principal, there is no way to answer
"which of my children triggered this?" after the fact, and the authorization
model is unauditable in exactly the situation it was built for.

It also matters outside federation: a `service` run and an owner-initiated run
are indistinguishable in the database today.
"""

from __future__ import annotations

import os
import uuid

import pytest

from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.tracking import create_run, get_run

pytestmark = pytest.mark.integration


def _db() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            db.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not os.environ.get("ROBOTHOR_DB_NAME") and not _db(), reason="no database"
)


@pytest.fixture
def cleanup_runs():
    made: list[str] = []
    yield made
    if not made:
        return
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute("DELETE FROM agent_runs WHERE id = ANY(%s)", (made,))
            db.commit()
    except Exception:
        pass


@requires_db
def test_a_run_records_the_principal_that_caused_it(cleanup_runs):
    run = AgentRun(
        id=str(uuid.uuid4()),
        agent_id="probe",
        trigger_type=TriggerType.FEDERATION,
        status=RunStatus.PENDING,
        user_id="federation:conn-1",
        user_role="federation_parent",
        tenant_id="default",
    )
    create_run(run)
    cleanup_runs.append(run.id)

    stored = get_run(run.id)
    assert stored is not None
    assert stored["user_role"] == "federation_parent", (
        "the run does not record which principal caused it — a parent's "
        "trigger is indistinguishable from any other run"
    )
    assert stored["user_id"] == "federation:conn-1"
    assert stored["tenant_id"] == "default"


@requires_db
def test_an_ordinary_run_still_records_its_role(cleanup_runs):
    """Not a federation-only fix: a `service` run and an owner-initiated run
    are indistinguishable in the database today."""
    run = AgentRun(
        id=str(uuid.uuid4()),
        agent_id="probe",
        trigger_type=TriggerType.MANUAL,
        status=RunStatus.PENDING,
        user_id="owner",
        user_role="owner",
    )
    create_run(run)
    cleanup_runs.append(run.id)

    assert get_run(run.id)["user_role"] == "owner"


@requires_db
def test_a_run_with_no_principal_is_stored_without_inventing_one(cleanup_runs):
    """Absent identity must read as absent, not as a plausible default."""
    run = AgentRun(id=str(uuid.uuid4()), agent_id="probe", status=RunStatus.PENDING)
    create_run(run)
    cleanup_runs.append(run.id)

    stored = get_run(run.id)
    assert stored["user_role"] in ("", None)
    assert stored["user_id"] in ("", None)
