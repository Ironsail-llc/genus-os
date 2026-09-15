"""``GET /api/controls/audit`` — the guardrail change log.

Every write through ``robothor.flags.store.set_flag`` appends a row to
``feature_flag_audit`` (migration 084): which flag, from what to what, by whom,
why, when. Nothing has ever read it back over HTTP, so the answer to "who put
RBAC into observe in August, and what reason did they give" lived only in psql.

**Why this is not in ``controls.py``.** That module is operator-only in all
four of its locks and its header says so in those words — no agent tool, bridge
not engine, no service tokens, platform tenant only, and "every other human
role (member, user, viewer, ``auditor``) is also 403'd". This route
deliberately admits an auditor, and a route that contradicts the docstring
above it is how a reader stops trusting either. So it lives beside that module
rather than inside it, on the same URL prefix, with its own gate named in its
own file.

**Why an auditor may read it.** ``auditor`` exists for exactly this: reviewing
what was done, without the authority to do any of it. Reading the change log is
the definition of the role, and the widening is bounded — this router has no
mutation route at all, and the auditor still cannot reach ``GET /api/controls``
(the current guardrail STATE), ``PATCH /api/controls`` (changing one), or any
other operator surface.

**No tenant filter, and that is the table's shape, not an omission.**
``feature_flags`` is a single GLOBAL table with no ``tenant_id`` column —
``controls.py``'s fourth lock exists precisely because of that — so the change
log is platform-wide and the gate restricts it to the platform tenant rather
than filtering rows that carry no tenant to filter on.

Handler is a plain ``def``: psycopg2 is synchronous, so FastAPI must run it in
its worker threadpool (see ``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from robothor.db.connection import get_connection
from routers._operator import require_audit_reader

router = APIRouter(prefix="/api/controls", tags=["controls"])

#: One page of change history. 500 matches ``/api/audit/events``, which is the
#: other log this page sits beside.
MAX_LIMIT = 500
DEFAULT_LIMIT = 50


def _positive_int(value: str, *, field: str, maximum: int | None = None) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field} must be a number") from None
    if parsed < 1 or (maximum is not None and parsed > maximum):
        bound = f" and {maximum}" if maximum is not None else ""
        raise HTTPException(status_code=422, detail=f"{field} must be between 1{bound}")
    return parsed


@router.get("/audit")
def list_flag_changes(
    request: Request,
    flag: str | None = Query(None, description="Only changes to this flag"),
    limit: str = Query(str(DEFAULT_LIMIT)),
    cursor: str | None = Query(None, description="Keyset cursor: the last id of the page before"),
) -> dict[str, Any]:
    """Guardrail changes, newest first.

    Keyset-paginated on ``id`` (a ``BIGSERIAL``, so descending id is exactly
    reverse insertion order) rather than OFFSET: the table is append-only and
    an operator reading page two while a flag is flipped would otherwise see a
    row twice.
    """
    require_audit_reader(request)
    page = _positive_int(limit, field="limit", maximum=MAX_LIMIT)
    after = _positive_int(cursor, field="cursor") if cursor is not None else None

    sql = (
        "SELECT id, name, from_value, to_value, actor, reason, at FROM feature_flag_audit WHERE 1=1"
    )
    params: list[Any] = []
    if flag:
        sql += " AND name = %s"
        params.append(flag)
    if after is not None:
        sql += " AND id < %s"
        params.append(after)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(page)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    changes = [
        {
            "id": int(row[0]),
            "flag": row[1],
            "old_value": row[2],
            "new_value": row[3],
            "changed_by": row[4],
            "reason": row[5],
            "changed_at": row[6].isoformat() if hasattr(row[6], "isoformat") else row[6],
        }
        for row in rows
    ]
    next_cursor = str(changes[-1]["id"]) if len(changes) == page else None
    return {"changes": changes, "next_cursor": next_cursor}
