"""Tests for the permission enforcement module."""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.permissions import check_tool_permission, resolve_accessible_tenants


class TestCheckToolPermission:
    """Tests for check_tool_permission()."""

    def test_empty_role_fails_closed(self):
        """Every execution path must provide a concrete human/service role."""
        result = check_tool_permission("", "test-tenant", "create_person")
        assert result is not None
        assert "denied" in result

    def test_no_rules_fails_closed(self):
        """No rules configured means fail-closed (denied)."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = []

            result = check_tool_permission("viewer", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result

    def test_deny_rule_blocks(self):
        """Matching deny rule blocks the tool."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [
                ("*", "deny", "__default__"),
            ]

            result = check_tool_permission("viewer", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result

    def test_allow_rule_permits(self):
        """Matching allow rule permits the tool."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [
                ("*", "allow", "__default__"),
            ]

            result = check_tool_permission("user", "test-tenant", "create_person")
            assert result is None

    def test_tenant_specific_deny_overrides_default_allow(self):
        """Tenant-specific deny wins over __default__ allow."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            # Tenant-specific policy takes precedence over platform defaults.
            mock_cursor.fetchall.return_value = [
                ("create_*", "deny", "test-tenant"),
                ("*", "allow", "__default__"),
            ]

            result = check_tool_permission("user", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result

    def test_viewer_can_search_but_not_create(self):
        """Viewer role with default rules: search allowed, create denied."""
        default_rules = [
            ("search_*", "allow", "__default__"),
            ("get_*", "allow", "__default__"),
            ("list_*", "allow", "__default__"),
            ("*", "deny", "__default__"),
        ]

        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = default_rules

            # Search should be allowed
            assert check_tool_permission("viewer", "t", "search_memory") is None
            # Create should be denied
            result = check_tool_permission("viewer", "t", "create_person")
            assert result is not None

    def test_specific_allow_beats_catchall_deny_regardless_of_row_order(self):
        """Database row order cannot turn a viewer allowlist into deny-all."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [
                ("*", "deny", "__default__"),
                ("search_*", "allow", "__default__"),
            ]

            assert check_tool_permission("viewer", "t", "search_memory") is None

    def test_deny_shadows_allow_same_tenant(self):
        """A deny rule at the same tenant level shadows a broader allow rule."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            # Same tenant: an exact deny is more specific than a wildcard allow.
            mock_cursor.fetchall.return_value = [
                ("create_person", "deny", "test-tenant"),
                ("*", "allow", "test-tenant"),
            ]

            result = check_tool_permission("user", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result

    def test_no_matching_rule_fails_closed(self):
        """Rules exist but none match the tool — fail-closed."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [
                ("unrelated_*", "allow", "__default__"),
            ]

            result = check_tool_permission("user", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result

    def test_db_error_fails_closed(self):
        """DB errors fail-closed (deny access)."""
        with patch("robothor.db.connection.get_connection", side_effect=Exception("DB down")):
            result = check_tool_permission("viewer", "test-tenant", "create_person")
            assert result is not None
            assert "denied" in result


class TestResolveAccessibleTenants:
    """Tests for resolve_accessible_tenants()."""

    def test_non_owner_gets_own_tenant_only(self):
        """Users and viewers only see their own tenant."""
        result = resolve_accessible_tenants("test-tenant", "user")
        assert result == ("test-tenant",)

    def test_empty_tenant_defaults(self):
        """Empty tenant_id returns DEFAULT_TENANT."""
        result = resolve_accessible_tenants("", "owner")
        assert len(result) == 1

    def test_owner_without_children_gets_own_only(self):
        """Owner in tenant with no children gets own tenant only."""
        with patch("robothor.engine.permissions._get_child_tenants", return_value=[]):
            result = resolve_accessible_tenants("parent", "owner")
            assert result == ("parent",)

    def test_owner_with_child_access_gets_children(self):
        """Owner gets own + child tenants via BFS traversal."""
        with patch("robothor.engine.permissions._get_child_tenants") as mock_children:
            # First call for "parent" returns two children, subsequent calls return none
            mock_children.side_effect = lambda tid: (
                ["child-1", "child-2"] if tid == "parent" else []
            )

            result = resolve_accessible_tenants("parent", "owner")
            assert result == ("parent", "child-1", "child-2")

    def test_admin_with_child_access(self):
        """Admin role also gets hierarchical access."""
        with patch("robothor.engine.permissions._get_child_tenants") as mock_children:
            mock_children.side_effect = lambda tid: ["child-1"] if tid == "parent" else []

            result = resolve_accessible_tenants("parent", "admin")
            assert result == ("parent", "child-1")

    def test_db_error_returns_own_tenant(self):
        """DB errors return just the user's own tenant."""
        with patch(
            "robothor.engine.permissions._get_child_tenants", side_effect=Exception("DB down")
        ):
            result = resolve_accessible_tenants("test-tenant", "owner")
            assert result == ("test-tenant",)


class TestCheckToolPermissionUserOverride:
    """Per-user permission overrides (Task 5, Unified Identity Context).

    A ``user_permissions`` row is the most-specific match and is evaluated
    BEFORE role rules; when no user rule matches the tool, the existing
    role-based logic runs completely unchanged. Omitting ``user_id``
    (the pre-Task-5 call shape) must skip the user-rule lookup entirely —
    every pre-existing caller of ``check_tool_permission`` is unaffected.
    """

    def test_no_user_id_skips_user_lookup_entirely(self):
        """Positional-only calls (the old signature) must issue exactly the
        same role query as before — no user_permissions round-trip at all."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [("*", "allow", "__default__")]

            result = check_tool_permission("user", "test-tenant", "create_person")

            assert result is None
            assert mock_cursor.execute.call_count == 1
            sql = mock_cursor.execute.call_args[0][0]
            assert "role_permissions" in sql
            assert "user_permissions" not in sql

    def test_user_deny_overrides_role_allow(self):
        """A user-level deny wins even though the role allows everything."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.side_effect = [
                [("delete_*", "deny")],  # user_permissions
                [("*", "allow", "__default__")],  # role_permissions (never needed)
            ]

            result = check_tool_permission(
                "owner", "test-tenant", "delete_task", user_id="user-1"
            )
            assert result is not None
            assert "denied" in result

    def test_user_allow_overrides_role_deny(self):
        """A user-level allow carves an exception out of a role-level deny."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.side_effect = [
                [("search_memory", "allow")],  # user_permissions
            ]

            result = check_tool_permission(
                "viewer", "test-tenant", "search_memory", user_id="user-1"
            )
            assert result is None

    def test_most_specific_user_rule_wins(self):
        """Same specificity metric as role rules: exact pattern beats glob,
        more literal characters beats fewer, deny wins a true tie."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.side_effect = [
                [("*", "allow"), ("delete_task", "deny")],  # user_permissions
            ]

            result = check_tool_permission(
                "owner", "test-tenant", "delete_task", user_id="user-1"
            )
            assert result is not None
            assert "denied" in result

    def test_no_matching_user_rule_falls_through_to_role_logic(self):
        """A user_id with rules that don't match the tool falls through to the
        unchanged role-based path — this must issue the second (role) query."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.side_effect = [
                [("send_email", "deny")],  # user_permissions — doesn't match
                [("*", "allow", "__default__")],  # role_permissions
            ]

            result = check_tool_permission(
                "user", "test-tenant", "create_person", user_id="user-1"
            )
            assert result is None
            assert mock_cursor.execute.call_count == 2

    def test_empty_user_id_skips_user_lookup(self):
        """An empty string is falsy — treated the same as omitted user_id."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [("*", "allow", "__default__")]

            result = check_tool_permission("user", "test-tenant", "create_person", user_id="")
            assert result is None
            assert mock_cursor.execute.call_count == 1

    def test_no_user_rules_at_all_falls_through(self):
        """user_id given but the user_permissions table has no rows for them
        — falls straight through to role logic (fail-closed if that's empty too)."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.side_effect = [
                [],  # user_permissions
                [],  # role_permissions
            ]

            result = check_tool_permission(
                "user", "test-tenant", "create_person", user_id="user-1"
            )
            assert result is not None
            assert "denied" in result

    def test_db_error_during_user_lookup_fails_closed(self):
        with patch(
            "robothor.db.connection.get_connection", side_effect=Exception("DB down")
        ):
            result = check_tool_permission(
                "owner", "test-tenant", "delete_task", user_id="user-1"
            )
            assert result is not None
            assert "denied" in result
