"""Tests for the WS-2 consolidation churn guard in `robothor.memory.lifecycle`.

Consolidation re-ate its own output (no source_type exclusion in the candidate
selector), which built 150-deep supersession chains and left ~80% of the table
inactive. These tests cover the guard logic without a live DB by mocking the
connection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import robothor.memory.lifecycle as lc
from robothor.memory.lifecycle import importance_floor


def _mock_conn() -> tuple[Any, Any]:
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


class TestGuardFlag:
    def test_default_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_CONSOLIDATION_GUARD", raising=False)
        assert lc._consolidation_guard_enabled() is False

    def test_on(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_CONSOLIDATION_GUARD", "1")
        assert lc._consolidation_guard_enabled() is True


class TestSupersession:
    def test_off_does_plain_updates_only(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_CONSOLIDATION_GUARD", raising=False)
        ctx, cur = _mock_conn()
        with patch.object(lc, "get_connection", return_value=ctx):
            lc._apply_consolidation_supersession(99, [1, 2, 3])
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert len(sqls) == 3  # one supersede per source, nothing else
        assert all("SET is_active = FALSE" in s for s in sqls)
        assert not any("importance_score" in s for s in sqls)

    def test_on_propagates_importance_and_caps_chain_depth(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_CONSOLIDATION_GUARD", "1")
        ctx, cur = _mock_conn()
        cur.fetchone.side_effect = [(0.8,)]  # MAX(source importance)
        # source 2 sits on an already-deep chain → must be skipped
        with (
            patch.object(lc, "get_connection", return_value=ctx),
            patch.object(lc, "_chain_depth", side_effect=lambda _cur, fid: 5 if fid == 2 else 0),
        ):
            lc._apply_consolidation_supersession(99, [1, 2])
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("GREATEST(importance_score" in s for s in sqls)  # importance propagated
        supersedes = [s for s in sqls if "SET is_active = FALSE" in s]
        assert len(supersedes) == 1  # source 2 (deep chain) skipped, source 1 superseded


class TestImportanceFloor:
    def test_security_and_resolution_floored(self) -> None:
        assert importance_floor("An unauthorized sign-in was detected on OpenRouter") == 0.7
        assert importance_floor("The login was confirmed legitimate and marked as closed") == 0.7
        assert importance_floor("the migration incident was resolved") == 0.7

    def test_routine_not_floored(self) -> None:
        assert importance_floor("Bob prefers tea over coffee") == 0.0
        assert importance_floor("") == 0.0
