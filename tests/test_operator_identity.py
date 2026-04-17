"""Tests for the operator-identity layer in robothor.crm.dal.

Covers:
  - get_owner_person(tenant_id) — tenant_users link, then owner.yaml fallback
  - bootstrap_owner_person_links() — idempotent, handles missing rows
  - search_people(prefer_owner=True) — owner row sorted first
  - resolve_contact() — owner priority on name-only lookups, channel IDs win
  - resolve_task() — requires_human uses owner.yaml identity

No live DB: ``get_connection`` is mocked to a stand-in cursor.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from robothor.constants import DEFAULT_TENANT
from robothor.owner_config import OwnerConfig


class FakeCursor:
    """Queue-driven cursor.

    Feed a sequence of ``(fetchone, fetchall)`` tuples; each ``execute`` pops
    the next one. ``None`` means "use previous" (handy for UPDATEs).
    """

    def __init__(self, script: list[tuple[Any, Any] | dict[str, Any]]):
        self._script = list(script)
        self._fetchone: Any = None
        self._fetchall: list[Any] = []
        self.executed: list[tuple[str, tuple]] = []
        self.rowcount = 0

    def execute(self, sql, params=()):  # noqa: D401 — mock signature
        self.executed.append((sql, tuple(params) if params else ()))
        if not self._script:
            self._fetchone = None
            self._fetchall = []
            return
        step = self._script.pop(0)
        if isinstance(step, dict):
            self._fetchone = step.get("fetchone")
            self._fetchall = step.get("fetchall", [])
            self.rowcount = step.get("rowcount", 0)
        else:
            self._fetchone, self._fetchall = step

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _patched_conn(cursor: FakeCursor):
    conn = FakeConnection(cursor)
    with patch("robothor.crm.dal.get_connection") as get_conn:
        get_conn.return_value = conn
        yield conn


# ─── get_owner_person ────────────────────────────────────────────────────────


class TestGetOwnerPerson:
    def test_returns_row_via_tenant_users_link(self):
        cur = FakeCursor(
            [
                (
                    {
                        "id": "owner-uuid",
                        "first_name": "Alice",
                        "last_name": "Example",
                        "email": "a@example.com",
                        "tenant_id": DEFAULT_TENANT,
                    },
                    [],
                ),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import get_owner_person

            row = get_owner_person(DEFAULT_TENANT)

        assert row["id"] == "owner-uuid"
        assert row["name"]["firstName"] == "Alice"

    def test_falls_back_to_owner_yaml_email_when_link_missing(self, monkeypatch):
        cur = FakeCursor(
            [
                (None, []),  # tenant_users join returns nothing
                (
                    {
                        "id": "fallback-uuid",
                        "first_name": "Alice",
                        "last_name": "E",
                        "email": "a@example.com",
                        "tenant_id": DEFAULT_TENANT,
                    },
                    [],
                ),  # email lookup hits
            ]
        )

        fake_cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="E",
            email="a@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: fake_cfg)

        with _patched_conn(cur):
            from robothor.crm.dal import get_owner_person

            row = get_owner_person(DEFAULT_TENANT)

        assert row["id"] == "fallback-uuid"

    def test_returns_empty_when_nothing_configured(self, monkeypatch):
        cur = FakeCursor([(None, [])])
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: None)
        with _patched_conn(cur):
            from robothor.crm.dal import get_owner_person

            row = get_owner_person(DEFAULT_TENANT)

        assert row == {"id": None}

    def test_ignores_config_for_other_tenant(self, monkeypatch):
        cur = FakeCursor([(None, [])])
        fake_cfg = OwnerConfig(
            tenant_id="other-tenant",
            first_name="Alice",
            last_name="E",
            email="a@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: fake_cfg)
        with _patched_conn(cur):
            from robothor.crm.dal import get_owner_person

            row = get_owner_person(DEFAULT_TENANT)

        assert row == {"id": None}


# ─── bootstrap_owner_person_links ────────────────────────────────────────────


class TestBootstrap:
    def test_no_config_is_noop(self, monkeypatch):
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: None)
        from robothor.crm.dal import bootstrap_owner_person_links

        result = bootstrap_owner_person_links()
        assert result["linked"] is False
        assert result["reason"] == "no owner config"

    def test_already_linked_is_noop(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"id": "tu-1", "person_id": "p-1"}, []),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is False
        assert result["reason"] == "already linked"
        assert result["person_id"] == "p-1"

    def test_links_existing_person_row(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"id": "tu-1", "person_id": None}, []),  # tenant_users SELECT
                ({"id": "existing-person"}, []),  # crm_people lookup
                {"fetchone": None, "fetchall": [], "rowcount": 1},  # UPDATE
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is True
        assert result["created_person"] is False
        assert result["person_id"] == "existing-person"

    def test_creates_person_when_missing(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)
        monkeypatch.setattr("robothor.crm.dal.create_person", lambda *a, **k: "new-person-uuid")

        cur = FakeCursor(
            [
                ({"id": "tu-1", "person_id": None}, []),  # tenant_users SELECT
                (None, []),  # crm_people lookup miss
                {"fetchone": None, "fetchall": [], "rowcount": 1},  # UPDATE
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is True
        assert result["created_person"] is True
        assert result["person_id"] == "new-person-uuid"

    def test_no_owner_tenant_user_is_noop(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor([(None, [])])  # no tenant_users owner row
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is False
        assert "no tenant_users row" in result["reason"]

    def test_update_scoped_to_selected_owner_row_id(self, monkeypatch):
        """Regression: the UPDATE must target the single owner row selected
        by SELECT ... LIMIT 1. If multiple unlinked owner rows exist (the
        state scripts/merge_operator_duplicates.py repairs), an unscoped
        UPDATE would assign them the same person_id and trip the partial
        unique index uq_tenant_users_owner_person from migration 039."""
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"id": "tu-42", "person_id": None}, []),  # tenant_users SELECT → id 42
                ({"id": "existing-person"}, []),  # crm_people lookup
                {"fetchone": None, "fetchall": [], "rowcount": 1},  # UPDATE
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is True
        # UPDATE should scope by id=tu-42, not by (tenant_id, role, person_id IS NULL)
        update_sql, update_params = cur.executed[-1]
        assert "UPDATE tenant_users" in update_sql
        assert "WHERE id = %s" in update_sql
        assert "person_id IS NULL" not in update_sql  # the old unscoped predicate
        assert update_params == ("existing-person", "tu-42")

    def test_created_person_flag_not_set_when_create_blocked(self, monkeypatch):
        """Regression: created_person=True was previously set before the
        success check, producing a result with both created_person=True AND
        reason='create_person was blocked'. Fix keeps the flag false on
        failure so audit/log consumers aren't misled."""
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT, first_name="Alice", last_name="E", email="a@example.com"
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)
        monkeypatch.setattr("robothor.crm.dal.create_person", lambda *a, **k: None)

        cur = FakeCursor(
            [
                ({"id": "tu-1", "person_id": None}, []),  # tenant_users SELECT
                (None, []),  # crm_people lookup miss → create_person is called
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import bootstrap_owner_person_links

            result = bootstrap_owner_person_links()

        assert result["linked"] is False
        assert result["created_person"] is False
        assert result["reason"] == "create_person was blocked"


# ─── search_people(prefer_owner=True) ────────────────────────────────────────


class TestSearchPeoplePreferOwner:
    def test_prefer_owner_injects_owner_id_into_order_by(self, monkeypatch):
        # First call inside get_owner_person returns the owner row.
        cur = FakeCursor(
            [
                (
                    {
                        "id": "owner-uuid",
                        "first_name": "Alice",
                        "last_name": "E",
                        "email": "a@example.com",
                        "tenant_id": DEFAULT_TENANT,
                    },
                    [],
                ),
                (None, []),  # search fetchall
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import search_people

            search_people("Alice", prefer_owner=True)

        # The second execute is the search query — verify owner-id binding.
        assert len(cur.executed) == 2
        search_sql, search_params = cur.executed[1]
        assert "CASE WHEN p.id = %s" in search_sql
        # Last bound param is the owner id.
        assert search_params[-1] == "owner-uuid"

    def test_prefer_owner_falls_back_to_default_order_when_no_owner(self, monkeypatch):
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: None)
        cur = FakeCursor(
            [
                (None, []),  # get_owner_person tenant_users join miss
                (None, []),  # search fetchall
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import search_people

            search_people("Alice", prefer_owner=True)

        # The search query should be the plain (non-prefer) variant.
        assert len(cur.executed) == 2
        search_sql, _ = cur.executed[-1]
        assert "CASE WHEN" not in search_sql


# ─── list_people delegates owner-priority ──────────────────────────────────


class TestListPeopleOwnerPriority:
    def test_list_people_with_search_uses_prefer_owner(self, monkeypatch):
        """Agent-facing list_people must route name searches through owner-preferring order."""
        captured: dict[str, Any] = {}

        def fake_search_people(name, tenant_id=DEFAULT_TENANT, prefer_owner=False):
            captured["prefer_owner"] = prefer_owner
            captured["name"] = name
            captured["tenant_id"] = tenant_id
            return []

        monkeypatch.setattr("robothor.crm.dal.search_people", fake_search_people)

        from robothor.crm.dal import list_people

        list_people(search="Alice", tenant_id=DEFAULT_TENANT)

        assert captured["prefer_owner"] is True
        assert captured["name"] == "Alice"


# ─── resolve_contact owner-priority ─────────────────────────────────────────


class TestResolveContactOwnerPriority:
    def test_name_only_lookup_prefers_owner_on_collision(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        # Flow:
        #  1. contact_identifiers SELECT — miss
        #  2. get_owner_person tenant_users JOIN — owner row
        #  3. contact_identifiers UPSERT — returns the mapping row
        cur = FakeCursor(
            [
                (None, []),
                (
                    {
                        "id": "owner-id",
                        "first_name": "Alice",
                        "last_name": "Owner",
                        "email": "alice@example.com",
                        "tenant_id": DEFAULT_TENANT,
                    },
                    [],
                ),
                ({"channel": "mention", "identifier": "msg-1", "person_id": "owner-id"}, []),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import resolve_contact

            result = resolve_contact(
                channel="mention",
                identifier="msg-1",
                name="Alice",
                tenant_id=DEFAULT_TENANT,
            )

        assert result["person_id"] == "owner-id"
        # Verify the upsert param had the owner id, not a search result.
        upsert_sql, upsert_params = cur.executed[-1]
        assert "INSERT INTO contact_identifiers" in upsert_sql
        assert "owner-id" in upsert_params

    def test_existing_channel_identifier_overrides_name_match(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        # Existing contact_identifiers row already maps to a non-owner Alice.
        cur = FakeCursor(
            [
                (
                    {
                        "channel": "telegram",
                        "identifier": "tg-999",
                        "person_id": "non-owner-id",
                        "display_name": "Alice Example",
                    },
                    [],
                ),
                # upsert
                (
                    {
                        "channel": "telegram",
                        "identifier": "tg-999",
                        "person_id": "non-owner-id",
                    },
                    [],
                ),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import resolve_contact

            result = resolve_contact(
                channel="telegram",
                identifier="tg-999",
                name="Alice",
                tenant_id=DEFAULT_TENANT,
            )

        assert result["person_id"] == "non-owner-id"
        # Owner lookup path should not have been used — only 2 queries total.
        assert len(cur.executed) == 2

    def test_non_owner_name_uses_search(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        # Flow for non-owner name "Charlie":
        #  1. contact_identifiers SELECT — miss
        #  2. search_people SELECT — returns Charlie row
        #  3. upsert
        cur = FakeCursor(
            [
                (None, []),
                (
                    None,
                    [
                        {
                            "id": "charlie-id",
                            "first_name": "Charlie",
                            "last_name": "Nonowner",
                            "email": "charlie@example.com",
                            "tenant_id": DEFAULT_TENANT,
                        }
                    ],
                ),
                ({"channel": "mention", "identifier": "msg-2", "person_id": "charlie-id"}, []),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import resolve_contact

            result = resolve_contact(
                channel="mention",
                identifier="msg-2",
                name="Charlie",
                tenant_id=DEFAULT_TENANT,
            )

        assert result["person_id"] == "charlie-id"


# ─── resolve_task human-approval gate ───────────────────────────────────────


class TestResolveTaskOwnerGate:
    def test_rejects_non_owner_agent_on_requires_human(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
            nicknames=frozenset({"ali"}),
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"status": "IN_PROGRESS", "requires_human": True}, []),
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import resolve_task

            result = resolve_task(task_id="t-1", resolution="done", agent_id="worker-bot")

        assert "error" in result
        assert "owner" in result["error"].lower()

    def test_accepts_owner_first_name(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"status": "IN_PROGRESS", "requires_human": True}, []),
                {"fetchone": None, "fetchall": [], "rowcount": 1},  # UPDATE
            ]
        )
        with _patched_conn(cur):
            from robothor.crm.dal import resolve_task

            # create a minimal tenants row SELECT? — resolve_task calls
            # _record_transition which issues additional queries; patch that out.
            with patch("robothor.crm.dal._record_transition"):
                result = resolve_task(task_id="t-1", resolution="done", agent_id="alice")

        # resolve_task returns True on success, dict{error:...} on failure.
        assert result is True

    def test_accepts_owner_nickname(self, monkeypatch):
        cfg = OwnerConfig(
            tenant_id=DEFAULT_TENANT,
            first_name="Alice",
            last_name="Owner",
            email="alice@example.com",
            nicknames=frozenset({"ali", "lisa"}),
        )
        monkeypatch.setattr("robothor.owner_config.load_owner_config", lambda: cfg)

        cur = FakeCursor(
            [
                ({"status": "IN_PROGRESS", "requires_human": True}, []),
                {"fetchone": None, "fetchall": [], "rowcount": 1},
            ]
        )
        with _patched_conn(cur), patch("robothor.crm.dal._record_transition"):
            from robothor.crm.dal import resolve_task

            result = resolve_task(task_id="t-1", resolution="done", agent_id="lisa")

        assert result is True
