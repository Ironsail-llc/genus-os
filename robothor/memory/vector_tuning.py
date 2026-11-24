"""Per-query HNSW session tuning for pgvector similarity searches.

Centralizes the ``SET LOCAL`` knobs applied before every HNSW vector query so
all search sites share one policy:

- ``hnsw.ef_search`` — candidate budget (default 100, env ``MEMORY_HNSW_EF_SEARCH``).
- ``hnsw.iterative_scan`` — pgvector >= 0.8: keep fetching past the ``WHERE``
  filter until ``LIMIT`` is met. Without it, a search under a narrow tenant or
  person filter spends the whole ``ef_search`` budget on other rows, drops them
  in the post-filter and returns fewer rows than exist (migration 111 has the
  history for the ``is_active`` case). **On by default** whenever the installed
  pgvector supports it — the version is read once per process from
  ``pg_extension``. An older build is never sent the GUC: inside a transaction
  an unrecognised ``hnsw.*`` parameter aborts the caller's next query, so a
  suppressed error would not be harmless. ``MEMORY_HNSW_ITERATIVE=0`` turns it
  off; no setting turns it on for an unsupported build.
  Mode is ``strict_order``: every caller orders by distance and some return the
  rows as-is, so exact order is kept rather than ``relaxed_order``'s speed.
- ``hnsw.max_scan_tuples`` — ceiling for an iterative scan (env
  ``MEMORY_HNSW_MAX_SCAN_TUPLES``, default 20000 — pgvector's own default, set
  explicitly so the bound is visible here). A scan under a filter matching a
  fraction ``f`` of the active rows reads about ``LIMIT / f`` tuples, so 20000
  serves a 30-row leg down to a tenant holding ~0.15% of the index, while
  bounding the worst case (a filter matching nothing) to a few milliseconds.

``SET LOCAL`` only takes effect inside a transaction — all callers use
``get_connection()`` (non-autocommit), which wraps each unit of work in one.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import Any

_OFF = frozenset({"0", "false", "no", "off"})

# pgvector release that introduced hnsw.iterative_scan / hnsw.max_scan_tuples.
ITERATIVE_SCAN_MIN_VERSION = (0, 8, 0)
ITERATIVE_SCAN_MODE = "strict_order"

_version_cache: dict[str, tuple[int, ...]] = {}


def _reset_pgvector_version_cache() -> None:
    _version_cache.clear()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(1, int(raw)) if raw else default
    except ValueError:
        return default


def _parse_version(raw: Any) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", str(raw or ""))
    if not match:
        return None
    return tuple(int(p) for p in match.groups(default="0"))


def pgvector_version(cur: Any) -> tuple[int, ...] | None:
    """The installed pgvector version, probed once per process (None if unknown).

    Only a successful probe is cached, so a transient failure is retried on the
    next search instead of disabling iterative scan for the life of the process.
    """
    cached = _version_cache.get("v")
    if cached is not None:
        return cached
    try:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    raw = row.get("extversion") if isinstance(row, dict) else row[0]
    version = _parse_version(raw)
    if version is not None:
        _version_cache["v"] = version
    return version


def hnsw_ef_search() -> int:
    """HNSW candidate budget per vector query (default 100)."""
    return _env_int("MEMORY_HNSW_EF_SEARCH", 100)


def iterative_scan_enabled(version: tuple[int, ...] | None) -> bool:
    """Iterative scan: on when ``version`` supports it, unless the env turns it off."""
    if os.environ.get("MEMORY_HNSW_ITERATIVE", "").strip().lower() in _OFF:
        return False
    return version is not None and version >= ITERATIVE_SCAN_MIN_VERSION


def max_scan_tuples() -> int:
    """Upper bound on tuples an iterative scan will examine (default 20000)."""
    return _env_int("MEMORY_HNSW_MAX_SCAN_TUPLES", 20000)


def apply_hnsw_session(cur: Any) -> None:
    """Apply SET LOCAL HNSW tuning on an open cursor (inside a transaction).

    Never raises into the caller's query path.
    """
    with contextlib.suppress(Exception):
        cur.execute("SET LOCAL hnsw.ef_search = %s", (hnsw_ef_search(),))
    if iterative_scan_enabled(pgvector_version(cur)):
        with contextlib.suppress(Exception):
            cur.execute(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN_MODE}")
        with contextlib.suppress(Exception):
            cur.execute("SET LOCAL hnsw.max_scan_tuples = %s", (max_scan_tuples(),))
