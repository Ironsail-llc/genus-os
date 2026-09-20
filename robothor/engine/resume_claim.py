"""One startup worker may resume a source run at a time.

A dedicated connection owns the session lock; it is never returned to a pool
with a lock held. Normal exit, cancellation and process death release ownership.
"""

import json
import logging
from contextvars import ContextVar

import psycopg2

logger = logging.getLogger(__name__)
current: ContextVar[object | None] = ContextVar("startup_resume_claim", default=None)


def acquire(tenant, run_id):
    from robothor.db.connection import get_connection

    connection = None
    try:
        with get_connection() as source:
            dsn = source.dsn
        connection = psycopg2.connect(dsn, connect_timeout=5)
        connection.autocommit = True
        with connection.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                (json.dumps(["runtime-resume", tenant, run_id]),),
            )
            if cur.fetchone()[0]:
                return connection
    except Exception as exc:
        logger.warning("Resume claim unavailable (%s)", type(exc).__name__)
    if connection is not None:
        connection.close()
    return None


def require_owned():
    """A disconnected session no longer owns the startup admission lock."""
    connection = current.get()
    if connection is None:
        return
    from robothor.engine.request_budget import RequestBudgetError

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
    except Exception as exc:
        raise RequestBudgetError("Resume claim lost; reconcile before further execution") from exc
