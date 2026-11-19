"""An agent reads only its own tenant's runs, even where row-level security is off.

``get_agent_run`` and ``classify_run_failure`` look a run up by id. With
``ROBOTHOR_RLS_ENABLED`` unset nothing at the database stops a run id from
another tenant answering, so the read itself names the tenant. The fake
database here behaves like a table with RLS off: it filters only on what the
query says.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from robothor.engine.tools.handlers import observability as obs

PARENT, CHILD_A = "acme-parent", "acme-child-a"
RUN_ID = "33333333-3333-3333-3333-333333333333"
ROWS = [
    {
        "id": RUN_ID,
        "tenant_id": CHILD_A,
        "agent_id": "triage",
        "status": "failed",
        "error_message": "a child's private error text",
    }
]


class _Cursor:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if "agent_runs" not in sql:
            self._rows = []
            return
        rows = [r for r in ROWS if r["id"] == params[0]]
        if "tenant_id" in sql:
            rows = [r for r in rows if r["tenant_id"] == params[1]]
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Conn:
    def cursor(self, **_: Any) -> _Cursor:
        return _Cursor()

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


@pytest.fixture
def db(monkeypatch):
    import robothor.engine.tracking as tracking

    monkeypatch.setattr(tracking, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(tracking, "list_steps", MagicMock(return_value=[]))


def _ctx(tenant_id: str) -> MagicMock:
    ctx = MagicMock()
    ctx.tenant_id = tenant_id
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["_get_agent_run", "_classify_run_failure"])
async def test_a_run_of_another_tenant_is_not_found(db, handler):
    out = await getattr(obs, handler)({"run_id": RUN_ID}, _ctx(PARENT))

    assert "error" in out and "not found" in out["error"].lower()
    assert "private error" not in str(out)


@pytest.mark.asyncio
async def test_a_run_of_the_callers_own_tenant_is_found(db):
    out = await obs._get_agent_run({"run_id": RUN_ID}, _ctx(CHILD_A))

    assert out["run"]["id"] == RUN_ID


def test_get_run_without_a_tenant_is_unchanged_for_engine_callers(db):
    from robothor.engine.tracking import get_run

    assert get_run(RUN_ID) is not None
