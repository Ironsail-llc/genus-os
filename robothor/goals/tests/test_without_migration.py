"""The engine must work when migration 126 is absent.

``robothor.engine.daemon`` boots with pending migrations on purpose, and a
rollback of 126 puts a live instance here too. Probed at tail 125,
``list_agent_tasks``, the thread claim, ``store.enabled()`` and the per-tool
admission check were all BROKEN rather than degraded — every agent lost its
task list. These tests hold the pre-feature behaviour in place.
"""

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg2
import pytest

from robothor.goals import compat

# The columns the two core inbox queries actually read. Deliberately not the
# whole of 001..125: this fixture is "the schema without 126", and a hand-built
# minimum is what keeps it readable.
TASKS_DDL = """
CREATE TABLE crm_tasks (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'TODO',
    priority TEXT,
    tags TEXT[],
    assigned_to_agent TEXT,
    person_id UUID,
    parent_task_id UUID,
    requires_human BOOLEAN NOT NULL DEFAULT false,
    escalation_count INTEGER NOT NULL DEFAULT 0,
    sla_deadline_at TIMESTAMPTZ,
    follow_up_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.fixture(scope="module")
def database_at_tail_125(tmp_path_factory):
    binary = next(
        (
            p
            for p in (Path("/usr/lib/postgresql/18/bin"), Path("/usr/lib/postgresql/16/bin"))
            if (p / "initdb").exists()
        ),
        None,
    )
    if binary is None:
        found = shutil.which("initdb")
        if found:
            binary = Path(found).parent
        else:
            pytest.skip("PostgreSQL binaries required for disposable-cluster integration tests")
    root = tmp_path_factory.mktemp("nogoalpg")
    data = root / "data"
    socket = root / "socket"
    socket.mkdir()

    def command(name, *args):
        subprocess.run(
            [str(binary / name), *map(str, args)], check=True, capture_output=True, text=True
        )

    command("initdb", "-D", data, "-U", "goaltest", "--auth=trust", "--no-locale")
    command(
        "pg_ctl",
        "-D",
        data,
        "-l",
        root / "postgres.log",
        "-o",
        f"-F -h '' -k {socket}",
        "-w",
        "start",
    )
    try:
        command("createdb", "-h", socket, "-U", "goaltest", "pre_126_test")
        dsn = f"dbname=pre_126_test user=goaltest host={socket}"
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(TASKS_DDL)
        yield dsn
    finally:
        command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture
def pre_126(database_at_tail_125, monkeypatch):
    @contextmanager
    def connection():
        conn = psycopg2.connect(database_at_tail_125)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connection)
    monkeypatch.setattr("robothor.crm.dal.get_connection", connection)
    monkeypatch.setattr("robothor.goals.store.get_connection", connection)
    compat.reset_probe()
    yield database_at_tail_125
    compat.reset_probe()


def seed_task(dsn, tenant, **columns):
    task_id = str(uuid4())
    names = ",".join(columns)
    holders = ",".join(["%s"] * len(columns))
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO crm_tasks(id,tenant_id,{names}) VALUES (%s,%s,{holders})",
            (task_id, tenant, *columns.values()),
        )
    return task_id


def test_a_cursor_that_cannot_answer_is_not_remembered():
    """The probe's answer is cached for the whole process, so the first caller
    decides for every later one. A test double whose ``fetchone`` returns a
    mock or ``None`` is not an answer: cached, it silently told the rest of the
    process that migration 126 was absent, and eight goal tests went red behind
    a passing mock in an unrelated file.
    """
    from unittest.mock import MagicMock

    compat.reset_probe()
    try:
        for answer in (None, MagicMock(), {"present": MagicMock()}, ("not a bool",)):
            cursor = MagicMock()
            cursor.fetchone.return_value = answer
            # Unknown keeps the predicate — the behaviour from before the probe.
            assert compat.pursuit_installed(cursor) is True
        # Nothing was remembered, so a cursor that CAN answer is still believed.
        honest = MagicMock()
        honest.fetchone.return_value = {"present": False}
        assert compat.pursuit_installed(honest) is False
    finally:
        compat.reset_probe()


def test_the_probe_reports_the_predicate_missing(pre_126):
    with psycopg2.connect(pre_126) as conn, conn.cursor() as cur:
        assert compat.pursuit_installed(cur) is False
        assert compat.task_gate(cur, "crm_tasks.id", "crm_tasks.tenant_id") == "TRUE"


def test_agent_task_inbox_still_lists_tasks_without_126(pre_126):
    from robothor.crm.dal import list_agent_tasks

    tenant = str(uuid4())
    seed_task(pre_126, tenant, title="Write the report", assigned_to_agent="main")
    assert [t["title"] for t in list_agent_tasks("main", tenant_id=tenant)] == ["Write the report"]


def test_thread_claim_still_returns_threads_without_126(pre_126):
    from robothor.engine.thread_pool import list_threads

    tenant = str(uuid4())
    seed_task(
        pre_126,
        tenant,
        title="Chase the vendor",
        assigned_to_agent="main",
        tags=["thread"],
        status="TODO",
    )
    assert [t.title for t in list_threads(tenant_id=tenant)] == ["Chase the vendor"]


def test_pursuit_reports_itself_off_rather_than_raising(pre_126):
    from robothor.goals import store
    from robothor.goals.runtime import task_runnable

    tenant = str(uuid4())
    assert store.enabled(tenant) is False
    assert task_runnable(str(uuid4()), tenant) is True


def test_admission_does_not_refuse_an_ordinary_task_without_126(pre_126):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.goals.runtime import admit_tool

    session = SimpleNamespace(run=SimpleNamespace(task_id=str(uuid4())))
    with patch("robothor.engine.session_registry.lookup", return_value=session):
        admit_tool("create_task", {}, ToolContext(run_id="worker", tenant_id=str(uuid4())))
