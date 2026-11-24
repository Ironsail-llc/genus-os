"""Tests for robothor.memory.vector_tuning.apply_hnsw_session.

Iterative index scan is already on for every pooled connection:
``robothor.db.connection`` sets ``hnsw.iterative_scan`` to
``ANN_ITERATIVE_SCAN_MODE`` (relaxed order, with the reasoning recorded there)
at checkout. ``apply_hnsw_session`` must not override that mode per
transaction; it sets the candidate budget, and only on the explicit
``MEMORY_HNSW_ITERATIVE`` opt-in adds the same mode plus a scan ceiling.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from robothor.db import connection
from robothor.memory import vector_tuning as vt


def _statements(cur: MagicMock) -> list[str]:
    return [c.args[0] for c in cur.execute.call_args_list]


class TestHelpers:
    def test_ef_search_default_and_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert vt.hnsw_ef_search() == 100
        with patch.dict(os.environ, {"MEMORY_HNSW_EF_SEARCH": "250"}):
            assert vt.hnsw_ef_search() == 250
        with patch.dict(os.environ, {"MEMORY_HNSW_EF_SEARCH": "junk"}):
            assert vt.hnsw_ef_search() == 100

    def test_iterative_opt_in_is_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert vt.iterative_scan_enabled() is False
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}):
            assert vt.iterative_scan_enabled() is True


class TestApplyHnswSession:
    def test_default_leaves_the_connection_scan_mode_alone(self) -> None:
        cur = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        assert any("hnsw.ef_search" in s for s in stmts)
        assert not any("iterative_scan" in s for s in stmts), (
            "a per-transaction SET LOCAL would override the mode every pooled "
            "connection already carries"
        )
        assert not any("pg_extension" in s for s in stmts)

    def test_opt_in_uses_the_connection_mode_never_a_second_one(self) -> None:
        cur = MagicMock()
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        modes = [s for s in stmts if "hnsw.iterative_scan" in s]
        assert modes, "the opt-in still sets the mode"
        assert all(connection.ANN_ITERATIVE_SCAN_MODE in s for s in modes)
        assert any("hnsw.max_scan_tuples" in s for s in stmts)

    def test_never_raises_when_guc_unsupported(self) -> None:
        cur = MagicMock()
        cur.execute.side_effect = Exception("unrecognized configuration parameter")
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)  # must not raise
