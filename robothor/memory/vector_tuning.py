"""Per-query HNSW session tuning for pgvector similarity searches.

Centralizes the ``SET LOCAL`` knobs applied before every HNSW vector query so
all search sites share one policy:

- ``hnsw.ef_search`` — candidate budget (default 100, env ``MEMORY_HNSW_EF_SEARCH``).
- ``hnsw.iterative_scan`` — pgvector >= 0.8: keep fetching past the ``WHERE``
  filter until ``LIMIT`` is met (the proper fix for post-filter recall collapse).
  The live build is 0.8.2 and the partial active index (migrations 073/074)
  already keeps dead vectors out of the budget; iterative scan is opt-in via
  ``MEMORY_HNSW_ITERATIVE`` (so an older build is never asked for an unsupported GUC).
- ``hnsw.max_scan_tuples`` — ceiling for iterative scan (env
  ``MEMORY_HNSW_MAX_SCAN_TUPLES``, default 20000).

Every ``SET`` is wrapped so an unsupported GUC (e.g. pre-0.8, or an autocommit
connection) is a silent no-op and never raises into the caller's query path.
``SET LOCAL`` only takes effect inside a transaction — all callers use
``get_connection()`` (non-autocommit), which wraps each unit of work in one.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(1, int(raw)) if raw else default
    except ValueError:
        return default


def hnsw_ef_search() -> int:
    """HNSW candidate budget per vector query (default 100)."""
    return _env_int("MEMORY_HNSW_EF_SEARCH", 100)


def iterative_scan_enabled() -> bool:
    """Whether to request pgvector 0.8 iterative scan. Default off."""
    return os.environ.get("MEMORY_HNSW_ITERATIVE", "").strip().lower() in _TRUE


def max_scan_tuples() -> int:
    """Upper bound on tuples an iterative scan will examine (default 20000)."""
    return _env_int("MEMORY_HNSW_MAX_SCAN_TUPLES", 20000)


def apply_hnsw_session(cur: Any) -> None:
    """Apply SET LOCAL HNSW tuning on an open cursor (inside a transaction).

    No-op-safe: any unsupported GUC is suppressed so callers' queries proceed
    unchanged on older pgvector builds.
    """
    with contextlib.suppress(Exception):
        cur.execute("SET LOCAL hnsw.ef_search = %s", (hnsw_ef_search(),))
    if iterative_scan_enabled():
        with contextlib.suppress(Exception):
            cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        with contextlib.suppress(Exception):
            cur.execute("SET LOCAL hnsw.max_scan_tuples = %s", (max_scan_tuples(),))
