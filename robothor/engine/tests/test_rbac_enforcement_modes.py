"""RBAC observe/enforce gate for system runs (Wave-1 hardening, PR-8).

classify_system_tool_access applies the agent's service_role under the
rbac_enforcement_mode ladder: off never checks; observe/alert log a would-deny
but allow; enforce blocks. Interactive RBAC (the dispatch user_role gate) is
untouched.
"""

from __future__ import annotations

import robothor.engine.permissions as perms


def _patch_verdict(monkeypatch, reason):
    """Make check_tool_permission return a fixed verdict (None=allow / str=deny)."""
    monkeypatch.setattr(perms, "check_tool_permission", lambda *a, **k: reason)


class TestClassifySystemToolAccess:
    def test_off_never_checks(self, monkeypatch):
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return "denied"

        monkeypatch.setattr(perms, "check_tool_permission", _spy)
        action, reason = perms.classify_system_tool_access("service", "t", "exec", "off")
        assert action == "allow"
        assert reason is None
        assert called["n"] == 0  # no DB check when off

    def test_allowed_when_permitted(self, monkeypatch):
        _patch_verdict(monkeypatch, None)
        assert perms.classify_system_tool_access("service", "t", "exec", "enforce") == (
            "allow",
            None,
        )

    def test_enforce_blocks_denied(self, monkeypatch):
        _patch_verdict(monkeypatch, "nope")
        action, reason = perms.classify_system_tool_access("ro", "t", "exec", "enforce")
        assert action == "block"
        assert reason == "nope"

    def test_observe_allows_but_reports_denial(self, monkeypatch):
        _patch_verdict(monkeypatch, "nope")
        assert perms.classify_system_tool_access("ro", "t", "exec", "observe") == (
            "observe",
            "nope",
        )

    def test_alert_behaves_like_observe(self, monkeypatch):
        _patch_verdict(monkeypatch, "nope")
        assert perms.classify_system_tool_access("ro", "t", "exec", "alert") == ("observe", "nope")
