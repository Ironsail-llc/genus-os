"""Tests for the permission enforcement module."""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.permissions import check_tool_permission, resolve_accessible_tenants


class TestCheckToolPermission:
    """Tests for check_tool_permission()."""

    def test_empty_role_always_allowed(self):
        """System/automated calls (empty role) skip permission checks."""
        result = check_tool_permission("", "test-tenant", "create_person")
        assert result is None

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
            # Tenant-specific deny first (ORDER BY tenant_id = test-tenant first)
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

    def test_deny_shadows_allow_same_tenant(self):
        """A deny rule at the same tenant level shadows a broader allow rule."""
        with patch("robothor.db.connection.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            # Same tenant, deny on specific tool sorts before allow on wildcard
            # (ORDER BY access DESC: 'deny' > 'allow')
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
