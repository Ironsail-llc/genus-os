"""Tests for optional row-level DataScope filtering in the CRM DAL (Task 5,
Unified Identity Context).

Every function under test gains an optional ``scope: DataScope | None = None``
keyword. ``scope=None`` (the default — every pre-existing caller) must issue
the exact same SQL as before Task 5: these tests pin that by asserting the
scope predicate text is ABSENT and the parameter list is unaffected. A
restricted scope adds an "own data + shared" predicate: rows linked to the
caller's own ``person_id``, or rows with no person linkage at all
(``person_id IS NULL``), stay visible; everything else is excluded from the
SQL. ``crm_people`` has no ``person_id`` column (it IS the person row), so
its own-row-only variant restricts to ``id = scope.person_id`` with no
org-general carve-out — there's no such thing as an unowned person.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robothor.identity.scope import DataScope

RESTRICTED = DataScope(tenant_id="tenant-a", person_id="person-1", restricted=True)
UNRESTRICTED = DataScope(tenant_id="tenant-a", person_id="person-1", restricted=False)


def _mock_conn(fetchone_return=None, fetchall_return=None):
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fetchone_return
    mock_cur.fetchall.return_value = fetchall_return if fetchall_return is not None else []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestGetPerson:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import get_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_person("person-1", tenant_id="tenant-a")

        sql, params = mock_cur.execute.call_args[0]
        assert "p.id = %s" in sql
        assert sql.count("p.id = %s") == 1
        assert params == ("person-1", "tenant-a")

    @patch("robothor.crm.dal.get_connection")
    def test_unrestricted_scope_unaffected(self, mock_get_conn):
        from robothor.crm.dal import get_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_person("person-1", tenant_id="tenant-a", scope=UNRESTRICTED)

        _, params = mock_cur.execute.call_args[0]
        assert params == ("person-1", "tenant-a")

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_own_row_predicate(self, mock_get_conn):
        from robothor.crm.dal import get_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_person("person-1", tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "person-1" in params
        assert sql.count("p.id = %s") == 2  # requested id + own-row restriction


class TestListPeople:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import list_people

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_people(tenant_id="tenant-a")

        sql, params = mock_cur.execute.call_args[0]
        assert "p.id" not in sql or "p.id = %s" not in sql
        assert "tenant-a" in params

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_restricts_to_own_row(self, mock_get_conn):
        from robothor.crm.dal import list_people

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_people(tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "p.id = %s" in sql
        assert "person-1" in params

    @patch("robothor.crm.dal.search_people")
    def test_search_branch_threads_scope(self, mock_search_people):
        from robothor.crm.dal import list_people

        mock_search_people.return_value = []
        list_people(search="Alice", tenant_id="tenant-a", scope=RESTRICTED)

        assert mock_search_people.call_args.kwargs.get("scope") is RESTRICTED


class TestSearchPeople:
    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_restricts_to_own_row(self, mock_get_conn):
        from robothor.crm.dal import search_people

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        search_people("Alice", tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "p.id = %s" in sql
        assert "person-1" in params

    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import search_people

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        search_people("Alice", tenant_id="tenant-a")

        sql, _params = mock_cur.execute.call_args[0]
        assert "p.id = %s" not in sql


class TestGetNote:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import get_note

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_note("note-1", tenant_id="tenant-a")

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id" not in sql
        assert list(params) == ["note-1", "tenant-a"]

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_own_plus_shared_predicate(self, mock_get_conn):
        from robothor.crm.dal import get_note

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_note("note-1", tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params


class TestListNotes:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import list_notes

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_notes(tenant_id="tenant-a")

        sql, _params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" not in sql

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_predicate(self, mock_get_conn):
        from robothor.crm.dal import list_notes

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_notes(tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params


class TestGetTask:
    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_predicate(self, mock_get_conn):
        from robothor.crm.dal import get_task

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_task("task-1", tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params

    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import get_task

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_task("task-1", tenant_id="tenant-a")

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id" not in sql
        assert list(params) == ["task-1", "tenant-a"]


class TestListTasks:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import list_tasks

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_tasks(tenant_id="tenant-a")

        sql, _params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" not in sql

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_predicate(self, mock_get_conn):
        from robothor.crm.dal import list_tasks

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_tasks(tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params


class TestListConversations:
    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import list_conversations

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_conversations(tenant_id="tenant-a")

        sql, _params = mock_cur.execute.call_args[0]
        assert "c.person_id = %s OR c.person_id IS NULL" not in sql

    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_predicate(self, mock_get_conn):
        from robothor.crm.dal import list_conversations

        mock_conn, mock_cur = _mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        list_conversations(tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "c.person_id = %s OR c.person_id IS NULL" in sql
        assert "person-1" in params


class TestGetConversation:
    @patch("robothor.crm.dal.get_connection")
    def test_restricted_scope_adds_predicate(self, mock_get_conn):
        from robothor.crm.dal import get_conversation

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_conversation(1, tenant_id="tenant-a", scope=RESTRICTED)

        sql, params = mock_cur.execute.call_args[0]
        assert "c.person_id = %s OR c.person_id IS NULL" in sql
        assert "person-1" in params

    @patch("robothor.crm.dal.get_connection")
    def test_scope_none_unaffected(self, mock_get_conn):
        from robothor.crm.dal import get_conversation

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        get_conversation(1, tenant_id="tenant-a")

        sql, params = mock_cur.execute.call_args[0]
        assert "c.person_id = %s OR c.person_id IS NULL" not in sql
        assert params == (1, "tenant-a")


class TestGetContact360:
    @patch("robothor.crm.dal.get_person")
    def test_restricted_scope_denies_other_persons_record(self, mock_get_person):
        from robothor.crm.dal import get_contact_360

        result = get_contact_360("someone-else", tenant_id="tenant-a", scope=RESTRICTED)

        assert "error" in result
        mock_get_person.assert_not_called()

    @patch("robothor.crm.dal.get_person_memory")
    @patch("robothor.crm.dal.get_person_notes")
    @patch("robothor.crm.dal.get_person_tasks")
    @patch("robothor.crm.dal.get_person_timeline")
    @patch("robothor.crm.dal.get_person_summary")
    @patch("robothor.crm.dal.get_person")
    def test_restricted_scope_allows_own_record(
        self,
        mock_get_person,
        mock_summary,
        mock_timeline,
        mock_tasks,
        mock_notes,
        mock_memory,
    ):
        from robothor.crm.dal import get_contact_360

        mock_get_person.return_value = {"id": "person-1"}
        mock_summary.return_value = {}
        mock_timeline.return_value = []
        mock_tasks.return_value = []
        mock_notes.return_value = []
        mock_memory.return_value = []

        result = get_contact_360("person-1", tenant_id="tenant-a", scope=RESTRICTED)

        assert "error" not in result
        mock_get_person.assert_called_once()

    @patch("robothor.crm.dal.get_person_memory")
    @patch("robothor.crm.dal.get_person_notes")
    @patch("robothor.crm.dal.get_person_tasks")
    @patch("robothor.crm.dal.get_person_timeline")
    @patch("robothor.crm.dal.get_person_summary")
    @patch("robothor.crm.dal.get_person")
    def test_scope_none_unaffected(
        self,
        mock_get_person,
        mock_summary,
        mock_timeline,
        mock_tasks,
        mock_notes,
        mock_memory,
    ):
        from robothor.crm.dal import get_contact_360

        mock_get_person.return_value = {"id": "someone-else"}
        mock_summary.return_value = {}
        mock_timeline.return_value = []
        mock_tasks.return_value = []
        mock_notes.return_value = []
        mock_memory.return_value = []

        result = get_contact_360("someone-else", tenant_id="tenant-a")

        assert "error" not in result
