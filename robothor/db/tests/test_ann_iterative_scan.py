"""Every pooled connection must ask pgvector to finish the job.

2026-08-27. ``memory_facts`` searches filter ``tenant_id`` and ``is_active``,
but the hnsw graph spans every tenant and every superseded row. pgvector
walks the graph first and applies those predicates AFTERWARDS, so the LIMIT
is consumed by candidates that are then discarded and the caller gets a short
result set. Measured on production, identical query, LIMIT 20:

    tenant robothor-primary (29,331 active)  -> 20 rows
    tenant delphi              (220 active)  -> 12 rows, 26 filtered away

Recall degraded in proportion to how small the tenant was -- the opposite of
what a multi-tenant memory system should do, and invisible, because a short
result set looks identical to a sparse corpus.

pgvector 0.8 added ``hnsw.iterative_scan``, which keeps scanning until the
limit is genuinely satisfied.
"""

from __future__ import annotations

from robothor.db import connection as dbc


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _Cur(self.sink)


def test_iterative_scan_is_set_on_checkout():
    sink: list[str] = []
    dbc._apply_ann_scan_mode(_Conn(sink))
    joined = " ".join(sink).lower()
    assert "hnsw.iterative_scan" in joined, (
        "connections do not enable iterative scans, so a filtered vector "
        "search returns fewer rows than its LIMIT"
    )
    assert "relaxed_order" in joined


def test_a_failure_never_breaks_the_checkout():
    """An older pgvector lacks the GUC; that must not take the pool down."""

    class _Boom:
        def cursor(self):
            raise RuntimeError("unrecognized configuration parameter")

    dbc._apply_ann_scan_mode(_Boom())  # must not raise


def test_it_is_applied_from_get_connection():
    """Wired, not merely defined -- the defect class this fixes is unwired guards.

    Reads the file rather than using ``inspect``: ``get_connection`` is
    wrapped by ``@contextmanager``, and linecache can serve stale source
    inside a test session, which would make this assertion lie in both
    directions.
    """
    import pathlib

    src = (pathlib.Path(dbc.__file__)).read_text()
    body = src[src.index("def get_connection(") :]
    assert "_apply_ann_scan_mode(conn)" in body
