"""Two env vars must not disagree about what "the tenant" is.

The Delphi engine unit sets ``Environment=ROBOTHOR_TENANT_ID=delphi`` but never
overrides ``ROBOTHOR_DEFAULT_TENANT``, which ``/etc/robothor/robothor.env`` pins
to ``robothor-primary``. So inside that process:

    ROBOTHOR_TENANT_ID     = delphi           <- what RLS binds the connection to
    ROBOTHOR_DEFAULT_TENANT = robothor-primary <- what DAL calls tag rows with

Every DAL call that took the default wrote a row tagged ``robothor-primary``
onto a connection bound to ``delphi``, and the RLS WITH CHECK refused it. The
failure is logged at WARNING and the caller gets None, so it never surfaced.

Measured 2026-08-22: 218 row-level-security refusals in the Delphi engine in
seven days, and ``memory_insights`` holds ZERO delphi rows against 2,858 for
robothor-primary. Delphi's memory writes have been discarded since 2026-07-14.

One env var caused it. The instance fix is one line in the unit; this is the
platform half, so that no instance can make the same mistake silently again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.constants import tenant_env_conflict

if TYPE_CHECKING:
    import pytest


class TestConflictDetection:
    def test_the_real_delphi_shape_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "delphi")
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "robothor-primary")
        conflict = tenant_env_conflict()
        assert conflict is not None
        assert "delphi" in conflict and "robothor-primary" in conflict

    def test_agreement_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "robothor-primary")
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "robothor-primary")
        assert tenant_env_conflict() is None

    def test_only_one_set_is_not_a_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single source of truth cannot disagree with itself."""
        monkeypatch.delenv("ROBOTHOR_DEFAULT_TENANT", raising=False)
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "delphi")
        assert tenant_env_conflict() is None

    def test_neither_set_is_not_a_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
        monkeypatch.delenv("ROBOTHOR_DEFAULT_TENANT", raising=False)
        assert tenant_env_conflict() is None

    def test_blank_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "delphi")
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "  ")
        assert tenant_env_conflict() is None

    def test_the_message_says_what_breaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A conflict the reader cannot act on is barely better than silence."""
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "delphi")
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "robothor-primary")
        conflict = tenant_env_conflict() or ""
        assert "RLS" in conflict or "row-level security" in conflict
        assert "ROBOTHOR_DEFAULT_TENANT" in conflict
