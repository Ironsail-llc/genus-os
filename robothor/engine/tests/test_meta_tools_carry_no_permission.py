"""The deferred meta-tools carry no permission of their own.

With deferral on by default, a member on an interactive run was refused a
granted tool twice over: the direct call to ``list_tasks`` got the deferred
unlock ("call it through tool_call"), and the ``tool_call`` was then refused
by the dispatch RBAC check because the stock member rows (``search_*``,
``get_*``, ``list_*`` allowed, ``*`` denied) never name ``tool_call``. Every
non-core tool the member was entitled to became unreachable.

``tool_search``, ``tool_describe`` and ``tool_call`` are permission-
transparent: RBAC on a ``tool_call`` is decided by the tool it wraps, which
the handler runs through the same dispatch and the same check a direct call
meets, so it grants nothing beyond that tool's own permission.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import permissions
from robothor.engine.permissions import check_tool_permission as _real_check
from robothor.engine.tools import dispatch
from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed
from robothor.engine.tools.handlers.toolsearch import HANDLERS as META_HANDLERS
from robothor.engine.wrapped_call import MalformedToolCallError, unwrap

#: The ``__default__`` rows a stock instance carries for these two roles.
STOCK_ROWS: dict[str, list[tuple[str, str, str]]] = {
    "member": [
        ("search_*", "allow", "__default__"),
        ("get_*", "allow", "__default__"),
        ("list_*", "allow", "__default__"),
        ("*", "deny", "__default__"),
    ],
    "owner": [("*", "allow", "__default__")],
}

GRANTED = "list_tasks"
NOT_PERMITTED = "gws_gmail_send"


def _stock_check(
    user_role: str, tenant_id: str, tool_name: str, *, user_id: str | None = None
) -> str | None:
    """The real evaluator, over the stock rows instead of a database."""
    with patch("robothor.db.connection.get_connection") as conn:
        cur = conn.return_value.__enter__.return_value.cursor.return_value
        cur.fetchall.return_value = STOCK_ROWS.get(user_role, [])
        return _real_check(user_role, tenant_id, tool_name, user_id=None)


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A deferred run granting both tools, with the stock RBAC rows."""
    seen: list[str] = []

    def _probe(name: str) -> Any:
        async def _handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen.append(name)
            return {"ok": True, "ran": name}

        return _handler

    handlers = {GRANTED: _probe(GRANTED), NOT_PERMITTED: _probe(NOT_PERMITTED), **META_HANDLERS}
    monkeypatch.setattr(permissions, "check_tool_permission", _stock_check)
    monkeypatch.setattr(dispatch, "_get_handlers", lambda: handlers)
    monkeypatch.setattr(dispatch, "_runtime_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatch, "_audit_tool_call", lambda *a, **k: None)
    token = set_deferred_allowed(frozenset({GRANTED, NOT_PERMITTED}))
    try:
        yield seen
    finally:
        clear_deferred_allowed(token)


async def _run(name: str, args: dict[str, Any], role: str = "member") -> dict[str, Any]:
    return await dispatch._execute_tool(
        name, args, tenant_id="tenant-a", user_id="user-1", user_role=role
    )


async def _wrapped(name: str, role: str = "member") -> dict[str, Any]:
    return await _run("tool_call", {"name": name, "arguments": {}}, role)


async def test_a_member_reaches_a_permitted_tool_through_tool_call(ran: list[str]) -> None:
    assert await _wrapped(GRANTED) == {"ok": True, "ran": GRANTED}
    assert ran == [GRANTED]


async def test_a_tool_the_role_may_not_use_is_refused_as_the_direct_call_is(
    ran: list[str],
) -> None:
    direct = await _run(NOT_PERMITTED, {})
    wrapped = await _wrapped(NOT_PERMITTED)
    assert "denied 'gws_gmail_send'" in direct["error"]
    assert wrapped == direct
    assert ran == []


async def test_a_malformed_tool_call_gets_the_handlers_refusal(ran: list[str]) -> None:
    with pytest.raises(MalformedToolCallError) as bad:
        unwrap({"arguments": {}})
    assert await _run("tool_call", {"arguments": {}}) == {"error": str(bad.value)}
    assert ran == []


@pytest.mark.parametrize(
    ("name", "args"), [("tool_search", {"query": "tasks"}), ("tool_describe", {"name": GRANTED})]
)
async def test_search_and_describe_need_no_permission_row(
    ran: list[str], name: str, args: dict[str, Any]
) -> None:
    result = await _run(name, args)
    assert "denied" not in str(result.get("error", ""))


async def test_an_owner_is_unchanged(ran: list[str]) -> None:
    assert await _wrapped(NOT_PERMITTED, role="owner") == {"ok": True, "ran": NOT_PERMITTED}
    assert await _run(NOT_PERMITTED, {}, role="owner") == {"ok": True, "ran": NOT_PERMITTED}


async def test_direct_calls_are_unchanged(ran: list[str]) -> None:
    assert await _run(GRANTED, {}) == {"ok": True, "ran": GRANTED}
    assert "denied" in (await _run(NOT_PERMITTED, {}))["error"]
    assert ran == [GRANTED]
