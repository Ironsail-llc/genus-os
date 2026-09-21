"""Bound acceptance delivery, including before interactive configuration lookup."""

import asyncio
import logging


async def emit(callback, context):
    from robothor.engine.runtime.deadlines import remaining

    if callback is None:
        return False
    left = remaining(context)
    if left is not None and left <= 0:
        return False
    try:
        await asyncio.wait_for(
            callback(
                {
                    "event": "accepted",
                    "request_id": context.request_id,
                    "phase": "accepted",
                    "text": "Request accepted",
                }
            ),
            timeout=min(1, left) if left is not None else 1,
        )
    except Exception:
        logging.getLogger(__name__).debug("Acceptance status delivery failed", exc_info=True)
    # One delivery attempt, even when the connection fails; later progress and
    # audit recovery retain ownership of this same request.
    return True


async def before_lookup(request):
    from robothor.engine.runtime.profile_admission import needs_lookup

    if not needs_lookup(request):
        return False
    try:
        return await emit(request.options.get("on_status"), request.context)
    except asyncio.CancelledError:
        from robothor.engine.runtime.admission_audit import record_interrupted, record_timeout
        from robothor.engine.runtime.deadlines import remaining

        left = remaining(request.context)
        if left is not None and left <= 0:
            await record_timeout(request)
        else:
            await record_interrupted(request)
        raise
