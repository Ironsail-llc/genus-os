"""Preserve an operator stop as cancellation, distinct from execution failure."""

import logging

from robothor.engine.runtime.provider_budget import DurableStopError
from robothor.engine.sanitize import sanitize_log


def failed_or_stopped(session, error, traceback):
    logger = logging.getLogger("robothor.engine.runner")
    if isinstance(error, DurableStopError):
        logger.warning("Agent %s cancelled by durable stop", sanitize_log(session.run.agent_id))
        session.record_error(str(error))
        return session.cancelled("Stopped as requested.")
    logger.error(
        "Agent %s failed: %s",
        sanitize_log(session.run.agent_id),
        sanitize_log(error),
        exc_info=True,
    )
    session.record_error(str(error), traceback)
    return session.fail(str(error), traceback)
