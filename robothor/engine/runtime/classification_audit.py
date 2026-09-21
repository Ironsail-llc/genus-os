"""Recoverable outcome for a request whose setup expired before its run was saved."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest

import asyncio
import logging
from dataclasses import replace

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


def _has_run(context: ExecutionContext) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM agent_runs WHERE tenant_id=%s
               AND runtime_context->>'request_id'=%s
               AND runtime_context->>'principal_id'=%s LIMIT 1""",
            (context.tenant_id, context.request_id, context.principal_id),
        )
        return bool(cur.fetchone())


async def record_timeout(request: RunRequest, deadline: datetime | None) -> None:
    from robothor.engine.models import RunStatus
    from robothor.engine.runtime.admission_audit import _record
    from robothor.engine.runtime.classification_window import REASON

    try:
        present = await asyncio.wait_for(asyncio.to_thread(_has_run, request.context), 1)
        if not present:
            bounded = replace(request, context=replace(request.context, deadline=deadline))
            await _record(bounded, RunStatus.TIMEOUT, REASON)
    except Exception as exc:
        logger.warning("Classification outcome audit unavailable (%s)", type(exc).__name__)
