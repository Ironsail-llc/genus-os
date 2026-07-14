"""RLS enabled + a superuser connection = RLS is INERT. Say so, loudly.

Postgres superusers bypass row-level security **unconditionally** — `ENABLE` and
`FORCE` are both ignored for them. So `ROBOTHOR_RLS_ENABLED=1` while connected as
a superuser is not "RLS on", it is RLS *off* with a flag that says on. That is
strictly worse than RLS off, because the operator believes they have isolation.

This is not theoretical, it happened twice on the same instance:

  * the engine originally connected as `philip` (a superuser), which is why
    migration 082 had to create the non-superuser `robothor_app` role at all;
  * `robothor-orchestrator` and `robothor-vision` never loaded the main config, so
    `config.py` fell back to `os.environ["USER"]` -> `philip` -> superuser. They
    bypassed RLS for as long as they have existed, while the instance reported RLS
    "enabled". Nothing anywhere said a word.

The default is the trap: `user=os.environ.get("ROBOTHOR_DB_USER", os.environ.get("USER", ...))`
quietly resolves to whoever runs the process — and on a single-box instance that
is usually an admin.

Detect it and make it loud. A guardrail that cannot fire is the bug this whole
hardening pass keeps finding; this is the same bug in the isolation layer.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robothor.db import connection as conn_mod


class _Cursor:
    def __init__(self, is_super: bool) -> None:
        self._is_super = is_super
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    def fetchone(self) -> tuple[Any, ...]:
        return (self._is_super,)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self, is_super: bool) -> None:
        self._is_super = is_super

    def cursor(self) -> _Cursor:
        return _Cursor(self._is_super)


@pytest.fixture(autouse=True)
def _rls_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "robothor-primary")


class TestASuperuserConnectionUnderRlsIsLoud:
    def test_it_logs_an_error_when_connected_as_a_superuser(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(conn_mod, "_warned_superuser", False, raising=False)
        with caplog.at_level(logging.ERROR):
            conn_mod._apply_tenant_scope(_Conn(is_super=True))  # type: ignore[arg-type]

        blob = " ".join(r.message.lower() for r in caplog.records)
        assert "superuser" in blob, (
            "connecting as a superuser with RLS enabled means RLS is INERT — "
            "Postgres ignores the policy entirely. That must be loud, or the "
            "operator believes they have tenant isolation they do not have."
        )

    def test_it_is_silent_for_a_normal_role(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(conn_mod, "_warned_superuser", False, raising=False)
        with caplog.at_level(logging.ERROR):
            conn_mod._apply_tenant_scope(_Conn(is_super=False))  # type: ignore[arg-type]

        assert not [r for r in caplog.records if "superuser" in r.message.lower()], (
            "a non-superuser role is the correct configuration — do not cry wolf"
        )

    def test_the_tenant_is_still_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The check must not replace the thing it is checking."""
        monkeypatch.setattr(conn_mod, "_warned_superuser", False, raising=False)
        c = _Conn(is_super=False)
        cursors: list[_Cursor] = []
        orig = c.cursor

        def _spy() -> _Cursor:
            cur = orig()
            cursors.append(cur)
            return cur

        c.cursor = _spy  # type: ignore[method-assign]
        conn_mod._apply_tenant_scope(c)  # type: ignore[arg-type]

        assert any("app.tenant_id" in s for cur in cursors for s in cur.executed), (
            "the connection must still be bound to its tenant"
        )
