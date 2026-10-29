"""Process logging setup, shared by every long-running entry point.

The engine has always routed stdlib logging through structlog in
``robothor.engine.daemon``. The bridge configured nothing, so its INFO lines
were dropped: with no handler on the root logger, the stdlib's last-resort
handler prints WARNING and above only. Both now use the functions here.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

__all__ = ["configure_structured_logging", "ensure_logging_configured", "select_log_renderer"]


def select_log_renderer() -> Any:
    """Pick the structlog renderer for this process.

    An explicit ``ROBOTHOR_LOG_FORMAT`` ("json" or "console") always wins.
    Without it, the dev ConsoleRenderer is used only when stdout is an
    interactive terminal; under systemd/journald the default is single-line
    JSON — the rich console renderer used to emit 224-line box-drawing
    tracebacks (with frame locals, including DSNs) per tool crash.
    """
    import structlog

    explicit = os.environ.get("ROBOTHOR_LOG_FORMAT", "").strip().lower()
    if explicit == "json":
        return structlog.processors.JSONRenderer()
    if explicit == "console":
        return structlog.dev.ConsoleRenderer()
    if sys.stdout.isatty():
        return structlog.dev.ConsoleRenderer()
    return structlog.processors.JSONRenderer()


def configure_structured_logging() -> None:
    """Route stdlib logging through structlog for the whole process, at INFO.

    Replaces whatever handlers the root logger had: the engine calls this as
    the first thing it does, and owns its process outright.
    """
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(processor=select_log_renderer()))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def ensure_logging_configured() -> bool:
    """Configure logging only if nothing has. Returns whether it did.

    For a service that may be started under something that already set
    logging up (a test runner, a uvicorn ``log_config``): an existing handler
    on the root logger is respected, never replaced. There is no log-level
    setting, so the level is INFO.
    """
    if logging.getLogger().handlers:
        return False
    configure_structured_logging()
    return True
