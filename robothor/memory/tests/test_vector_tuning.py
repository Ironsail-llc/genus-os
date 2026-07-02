"""Tests for robothor.memory.vector_tuning.apply_hnsw_session."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

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

    def test_iterative_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert vt.iterative_scan_enabled() is False
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}):
            assert vt.iterative_scan_enabled() is True


class TestApplyHnswSession:
    def test_ef_search_always_set_iterative_off(self) -> None:
        cur = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        assert any("hnsw.ef_search" in s for s in stmts)
        assert not any("iterative_scan" in s for s in stmts)

    def test_iterative_and_max_scan_set_when_enabled(self) -> None:
        cur = MagicMock()
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        assert any("hnsw.ef_search" in s for s in stmts)
        assert any("hnsw.iterative_scan" in s for s in stmts)
        assert any("hnsw.max_scan_tuples" in s for s in stmts)

    def test_never_raises_when_guc_unsupported(self) -> None:
        cur = MagicMock()
        cur.execute.side_effect = Exception("unrecognized configuration parameter")
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)  # must not raise
