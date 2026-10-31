"""Operator-confirmed facts: the shared predicate and the supersede guard.

An operator's edit marks a fact ``metadata.operator_confirmed = {at, by}``.
Every automatic path that retires facts must leave such a fact alone; these
tests cover the predicate those paths share and the defence in depth in the
one writer they all go through.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import robothor.memory.bitemporal as bt
from robothor.memory.confirmed import NOT_OPERATOR_CONFIRMED_SQL, is_operator_confirmed

_CONFIRMED = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "operator"}}


def _mock_conn(rowcount: int = 1) -> tuple[Any, Any]:
    cur = MagicMock()
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


class TestIsOperatorConfirmed:
    def test_a_confirmed_dict_is_confirmed(self) -> None:
        assert is_operator_confirmed({"id": 1, "metadata": _CONFIRMED}) is True

    def test_metadata_as_a_json_string_is_read(self) -> None:
        assert is_operator_confirmed({"metadata": json.dumps(_CONFIRMED)}) is True

    @pytest.mark.parametrize(
        "metadata",
        [None, {}, {"operator_confirmed": None}, "", "not json", "[]", {"other": 1}],
    )
    def test_anything_else_is_not_confirmed(self, metadata: Any) -> None:
        assert is_operator_confirmed({"id": 1, "metadata": metadata}) is False

    def test_a_row_without_metadata_is_not_confirmed(self) -> None:
        assert is_operator_confirmed({"id": 1}) is False


class TestSqlFragment:
    def test_the_fragment_is_the_jsonb_null_test(self) -> None:
        # Verified against Postgres: true for NULL metadata, {}, and an explicit
        # JSON null; false once the key holds a value.
        assert NOT_OPERATOR_CONFIRMED_SQL == (
            "COALESCE(metadata->'operator_confirmed', 'null'::jsonb) = 'null'::jsonb"
        )


class TestSupersedeWithValidityGuard:
    def test_the_update_refuses_a_confirmed_target_by_default(self) -> None:
        ctx, cur = _mock_conn(rowcount=1)
        with patch.object(bt, "get_connection", return_value=ctx):
            assert bt.supersede_with_validity(7, 9, tenant_id="test-tenant") is True
        first = cur.execute.call_args_list[0].args[0]
        assert "SET is_active = FALSE" in first
        assert NOT_OPERATOR_CONFIRMED_SQL in first

    def test_a_confirmed_target_is_a_no_op(self) -> None:
        # rowcount 0: the guard matched nothing, so the target is confirmed.
        ctx, cur = _mock_conn(rowcount=0)
        with patch.object(bt, "get_connection", return_value=ctx):
            assert bt.supersede_with_validity(7, 9, tenant_id="test-tenant") is False
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert len(sqls) == 1  # the successor's valid_from is left alone too

    def test_force_supersedes_a_confirmed_target(self) -> None:
        ctx, cur = _mock_conn(rowcount=1)
        with patch.object(bt, "get_connection", return_value=ctx):
            assert bt.supersede_with_validity(7, 9, tenant_id="test-tenant", force=True) is True
        first = cur.execute.call_args_list[0].args[0]
        assert NOT_OPERATOR_CONFIRMED_SQL not in first
        assert len(cur.execute.call_args_list) == 2


class TestKeepOverruledGuard:
    def test_it_never_touches_the_kept_or_a_confirmed_row(self) -> None:
        ctx, cur = _mock_conn(rowcount=1)
        with patch.object(bt, "get_connection", return_value=ctx):
            assert bt.keep_overruled(99, 7, tenant_id="test-tenant") is True
        sql, params = cur.execute.call_args.args
        assert "id <> %s" in sql
        assert NOT_OPERATOR_CONFIRMED_SQL in sql
        assert tuple(params) == (7, 99, "test-tenant", 7)

    def test_the_kept_id_itself_is_a_no_op(self) -> None:
        # store_fact's dedup can answer an existing active id; if that is the
        # confirmed fact, the guard matches nothing and nothing is deactivated.
        ctx, _cur = _mock_conn(rowcount=0)
        with patch.object(bt, "get_connection", return_value=ctx):
            assert bt.keep_overruled(7, 7, tenant_id="test-tenant") is False
