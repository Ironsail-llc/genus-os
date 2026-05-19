"""Tests for scripts/audit_autonomy_budgets.py — uses mocked DB connections."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch


@contextmanager
def _mock_connection(rows: list[tuple]):
    """Yield a fake context-managed DB connection whose cursor returns ``rows``."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    yield cm


class TestScan:
    def test_dict_budget_violations_use_validate_budget_reason(self):
        from scripts.audit_autonomy_budgets import _scan

        # Budget is a dict but has a negative reversible_cap_usd — validate_budget rejects.
        bad_dict = {"reversible_cap_usd": -1}
        rows = [("task-1", "negative cap", "default", bad_dict)]
        with _mock_connection(rows) as cm:
            with patch("scripts.audit_autonomy_budgets.get_connection", return_value=cm):
                result = _scan(tenant_id=None)
        assert len(result) == 1
        v = result[0]
        assert v["task_id"] == "task-1"
        assert v["title"] == "negative cap"
        assert v["tenant_id"] == "default"
        # The reason should come from validate_budget, not the not-a-dict path.
        assert "not a dict" not in v["reason"]

    def test_non_dict_budget_reported_as_typed_violation(self):
        """A legacy row carrying a string-typed autonomy_budget is a violation,
        not silently treated as ``{}``. The reason names the offending type so
        the operator can decide whether to delete the row, repair the JSONB
        cast, or open a ticket."""
        from scripts.audit_autonomy_budgets import _scan

        rows = [
            ("task-str", "string budget", "default", "irreversible_cap_usd: 100"),
            ("task-list", "list budget", "default", ["execute"]),
            ("task-int", "int budget", "default", 42),
            ("task-ok", "ok dict", "default", {}),  # empty dict — the SQL filter excludes
        ]
        # The SQL filter ``autonomy_budget <> '{}'::jsonb`` would already drop the
        # last row in production, but our mock returns whatever we hand it — the
        # validator should still pass on `{}`, so it doesn't show up as a violator.
        with _mock_connection(rows) as cm:
            with patch("scripts.audit_autonomy_budgets.get_connection", return_value=cm):
                result = _scan(tenant_id=None)
        # Three non-dict rows reported, plus the empty dict passes validate_budget.
        by_id = {v["task_id"]: v for v in result}
        assert by_id["task-str"]["reason"] == "not a dict: str"
        assert by_id["task-list"]["reason"] == "not a dict: list"
        assert by_id["task-int"]["reason"] == "not a dict: int"
        assert "task-ok" not in by_id  # empty dict is fine

    def test_empty_rowset_returns_no_violators(self):
        from scripts.audit_autonomy_budgets import _scan

        with _mock_connection([]) as cm:
            with patch("scripts.audit_autonomy_budgets.get_connection", return_value=cm):
                assert _scan(tenant_id="default") == []


class TestCleanMessageScopeLabel:
    def test_no_tenant_says_all_tenants(self, capsys):
        from scripts.audit_autonomy_budgets import main

        with (
            patch("scripts.audit_autonomy_budgets._scan", return_value=[]),
            patch("sys.argv", ["audit_autonomy_budgets.py"]),
        ):
            rc = main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "in all tenants" in out

    def test_explicit_tenant_does_not_read_as_in_default(self, capsys):
        """``--tenant default`` produced the misleading message "Clean — no
        autonomy_budget violations in default." Fix is to wrap the tenant
        name in repr() and prefix with the word 'tenant'."""
        from scripts.audit_autonomy_budgets import main

        with (
            patch("scripts.audit_autonomy_budgets._scan", return_value=[]),
            patch("sys.argv", ["audit_autonomy_budgets.py", "--tenant", "default"]),
        ):
            rc = main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "in tenant 'default'" in out
        assert "in default" not in out

    def test_custom_tenant_label_quoted_in_message(self, capsys):
        from scripts.audit_autonomy_budgets import main

        with (
            patch("scripts.audit_autonomy_budgets._scan", return_value=[]),
            patch("sys.argv", ["audit_autonomy_budgets.py", "--tenant", "ironsail-prod"]),
        ):
            rc = main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "in tenant 'ironsail-prod'" in out
