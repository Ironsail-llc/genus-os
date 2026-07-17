"""Tests for robothor.identity.scope — the "own data + shared" DataScope model.

Non-privileged identities (role not in {owner, admin, service}) may only
draw on rows linked to their own person_id, or rows with person_id IS NULL
(org-general). Owner/admin/service see everything in-tenant. identity=None
(service/system callers that never resolved an interactive identity) must
stay unrestricted — that is the pre-existing, unaffected behavior of every
cron/hook/heartbeat run.
"""

from __future__ import annotations

from robothor.identity.context import IdentityContext
from robothor.identity.scope import (
    DataScope,
    log_would_drop,
    observe_scope,
    rows_dropped_by_identity_scope,
    rows_dropped_by_scope,
    scope_for,
    scope_for_query,
)


def _identity(role: str, person_id: str | None = "person-1") -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-a",
        channel="webchat",
        identifier="user-1",
        verified=True,
        role=role,
        person_id=person_id,
    )


class TestScopeFor:
    def test_identity_none_is_unrestricted(self) -> None:
        scope = scope_for(None)
        assert scope == DataScope(tenant_id="", person_id=None, restricted=False)

    def test_owner_role_unrestricted(self) -> None:
        scope = scope_for(_identity("owner"))
        assert scope.restricted is False

    def test_admin_role_unrestricted(self) -> None:
        scope = scope_for(_identity("admin"))
        assert scope.restricted is False

    def test_service_role_unrestricted(self) -> None:
        scope = scope_for(_identity("service"))
        assert scope.restricted is False

    def test_member_role_restricted(self) -> None:
        scope = scope_for(_identity("member"))
        assert scope.restricted is True
        assert scope.person_id == "person-1"
        assert scope.tenant_id == "tenant-a"

    def test_viewer_role_restricted(self) -> None:
        assert scope_for(_identity("viewer")).restricted is True

    def test_missing_role_counts_as_restricted(self) -> None:
        """An identity that exists but carries no role must fail toward restricted,
        not toward the privileged/unrestricted default."""
        scope = scope_for(_identity(""))
        assert scope.restricted is True

    def test_restricted_identity_with_no_person_id(self) -> None:
        """A restricted caller with no linked person only ever matches
        person_id IS NULL rows — a valid, if narrow, scope."""
        scope = scope_for(_identity("member", person_id=None))
        assert scope.restricted is True
        assert scope.person_id is None


class TestScopeForQuery:
    """scope_for_query decides what a DAL call should receive: a real scope
    only under enforce mode AND only when the identity is actually restricted
    (owner/admin/service/None always pass scope=None — unrestricted query)."""

    def test_off_mode_never_returns_scope(self) -> None:
        assert scope_for_query("off", _identity("member")) is None

    def test_observe_mode_never_returns_scope(self) -> None:
        """Observe must never change the query — only enforce may."""
        assert scope_for_query("observe", _identity("member")) is None

    def test_enforce_mode_returns_scope_for_restricted_identity(self) -> None:
        scope = scope_for_query("enforce", _identity("member"))
        assert scope is not None
        assert scope.restricted is True

    def test_enforce_mode_returns_none_for_privileged_identity(self) -> None:
        assert scope_for_query("enforce", _identity("owner")) is None

    def test_enforce_mode_returns_none_for_no_identity(self) -> None:
        assert scope_for_query("enforce", None) is None


class TestObserveScope:
    """observe_scope is the mirror of scope_for_query for the observe rung:
    only returns a scope (for dry-run counting) when mode is observe AND the
    identity is restricted."""

    def test_off_mode_returns_none(self) -> None:
        assert observe_scope("off", _identity("member")) is None

    def test_enforce_mode_returns_none(self) -> None:
        """enforce already filters at the query — no separate dry-run needed."""
        assert observe_scope("enforce", _identity("member")) is None

    def test_observe_mode_returns_scope_for_restricted_identity(self) -> None:
        scope = observe_scope("observe", _identity("member"))
        assert scope is not None
        assert scope.restricted is True

    def test_observe_mode_returns_none_for_privileged_identity(self) -> None:
        assert observe_scope("observe", _identity("owner")) is None

    def test_observe_mode_returns_none_for_no_identity(self) -> None:
        assert observe_scope("observe", None) is None


class TestRowsDroppedByScope:
    """The 'own data + shared' predicate applied client-side for observe-mode
    counting: a row is dropped iff it has a person_id set AND it isn't the
    caller's own."""

    def test_unrestricted_scope_drops_nothing(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=False)
        rows = [{"person_id": "someone-else"}]
        assert rows_dropped_by_scope(rows, scope) == 0

    def test_own_rows_not_dropped(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"person_id": "person-1"}, {"person_id": "person-1"}]
        assert rows_dropped_by_scope(rows, scope) == 0

    def test_org_general_rows_not_dropped(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"person_id": None}]
        assert rows_dropped_by_scope(rows, scope) == 0

    def test_other_persons_rows_dropped(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"person_id": "person-2"}, {"person_id": "person-1"}, {"person_id": None}]
        assert rows_dropped_by_scope(rows, scope) == 1

    def test_custom_person_key(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"owner_person_id": "person-2"}]
        assert rows_dropped_by_scope(rows, scope, person_key="owner_person_id") == 1


class TestRowsDroppedByIdentityScope:
    """crm_people has no person_id column — it IS the person. The
    own-row-only variant: a row is dropped unless its id equals the scope's
    person_id (no org-general carve-out; there's no such thing as an
    unowned person row)."""

    def test_unrestricted_scope_drops_nothing(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=False)
        rows = [{"id": "person-2"}]
        assert rows_dropped_by_identity_scope(rows, scope) == 0

    def test_own_row_not_dropped(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"id": "person-1"}]
        assert rows_dropped_by_identity_scope(rows, scope) == 0

    def test_other_rows_dropped(self) -> None:
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        rows = [{"id": "person-1"}, {"id": "person-2"}]
        assert rows_dropped_by_identity_scope(rows, scope) == 1


class TestLogWouldDrop:
    def test_logs_when_dropped_positive(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        log_would_drop(
            tool_name="search_memory",
            user_id="user-9",
            scope=scope,
            dropped=3,
            table="memory_facts",
        )
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "data_scoping" in msg
        assert "would_drop=3" in msg
        assert "tool=search_memory" in msg
        assert "user=user-9" in msg
        assert "person=person-1" in msg
        assert "table=memory_facts" in msg

    def test_silent_when_nothing_dropped(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        scope = DataScope(tenant_id="t", person_id="person-1", restricted=True)
        log_would_drop(
            tool_name="search_memory", user_id="u", scope=scope, dropped=0, table="memory_facts"
        )
        assert len(caplog.records) == 0
