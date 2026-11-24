"""Tests for robothor.memory.vector_tuning.apply_hnsw_session.

Iterative index scan is the default wherever the installed pgvector supports
it (>= 0.8): without it a vector search under a narrow tenant/person filter
post-filters the HNSW candidate budget and returns fewer rows than asked for.
An older pgvector must never be sent the GUC — inside a transaction an
unrecognised parameter aborts the caller's query, so suppressing the error is
not enough.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.memory import vector_tuning as vt


@pytest.fixture(autouse=True)
def _fresh_version_cache() -> Any:
    vt._reset_pgvector_version_cache()
    yield
    vt._reset_pgvector_version_cache()


def _statements(cur: MagicMock) -> list[str]:
    return [c.args[0] for c in cur.execute.call_args_list]


def _cursor(version: str | None) -> MagicMock:
    """A cursor that answers the extension-version probe with ``version``."""
    cur = MagicMock()
    cur.fetchone.return_value = None if version is None else (version,)
    return cur


class TestHelpers:
    def test_ef_search_default_and_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert vt.hnsw_ef_search() == 100
        with patch.dict(os.environ, {"MEMORY_HNSW_EF_SEARCH": "250"}):
            assert vt.hnsw_ef_search() == 250
        with patch.dict(os.environ, {"MEMORY_HNSW_EF_SEARCH": "junk"}):
            assert vt.hnsw_ef_search() == 100

    def test_iterative_follows_the_installed_version_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert vt.iterative_scan_enabled((0, 8, 7)) is True
            assert vt.iterative_scan_enabled((0, 8, 0)) is True
            assert vt.iterative_scan_enabled((0, 7, 4)) is False
            assert vt.iterative_scan_enabled(None) is False

    def test_env_can_turn_it_off_but_never_on_for_an_old_build(self) -> None:
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "0"}):
            assert vt.iterative_scan_enabled((0, 8, 7)) is False
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}):
            assert vt.iterative_scan_enabled((0, 7, 4)) is False

    def test_version_parsing(self) -> None:
        assert vt._parse_version("0.8.7") == (0, 8, 7)
        assert vt._parse_version("0.8.0-dev") == (0, 8, 0)
        assert vt._parse_version("junk") is None


class TestApplyHnswSession:
    def test_iterative_scan_is_on_by_default_when_supported(self) -> None:
        cur = _cursor("0.8.7")
        with patch.dict(os.environ, {}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        assert any("hnsw.ef_search" in s for s in stmts)
        assert any("hnsw.iterative_scan" in s for s in stmts)
        assert any("hnsw.max_scan_tuples" in s for s in stmts)

    def test_old_pgvector_is_never_sent_the_guc(self) -> None:
        cur = _cursor("0.7.4")
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)
        stmts = _statements(cur)
        assert any("hnsw.ef_search" in s for s in stmts)
        assert not any("iterative_scan" in s for s in stmts)

    def test_unknown_version_keeps_iterative_off(self) -> None:
        cur = _cursor(None)
        with patch.dict(os.environ, {}, clear=True):
            vt.apply_hnsw_session(cur)
        assert not any("iterative_scan" in s for s in _statements(cur))

    def test_version_is_probed_once_per_process(self) -> None:
        cur = _cursor("0.8.7")
        with patch.dict(os.environ, {}, clear=True):
            vt.apply_hnsw_session(cur)
            vt.apply_hnsw_session(cur)
        probes = [s for s in _statements(cur) if "pg_extension" in s]
        assert len(probes) == 1

    def test_never_raises_when_guc_unsupported(self) -> None:
        cur = MagicMock()
        cur.execute.side_effect = Exception("unrecognized configuration parameter")
        with patch.dict(os.environ, {"MEMORY_HNSW_ITERATIVE": "1"}, clear=True):
            vt.apply_hnsw_session(cur)  # must not raise
