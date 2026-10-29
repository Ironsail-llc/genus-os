"""Process logging setup, shared by every long-running entry point.

The engine has always routed stdlib logging through structlog in
``robothor.engine.daemon``. The bridge configured nothing, so its INFO lines
were dropped: with no handler on the root logger, the stdlib's last-resort
handler prints WARNING and above only. Both now use the functions here.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

__all__ = [
    "CredentialRedactingFilter",
    "configure_structured_logging",
    "ensure_logging_configured",
    "redact_log_text",
    "select_log_renderer",
]

#: Libraries that log every request with its full URL at INFO. httpx does
#: (``HTTP Request: GET <url> "200 OK"``), and the setup wizard checks a
#: Telegram token at ``https://api.telegram.org/bot<token>/getMe`` — so a root
#: INFO handler would print each token an operator types into the journal.
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore")

#: A Telegram bot token in a URL path: ``/bot<digits>:<secret>``.
_BOT_TOKEN = re.compile(r"(/bot\d+:)[A-Za-z0-9_-]+")

#: A credential-bearing query (or fragment) parameter, by NAME.
_CREDENTIAL_PARAM = re.compile(
    r"([?&#;](?:access_token|api_key|token|key|secret|password)=)[^&#\s\"']*",
    re.IGNORECASE,
)

_REDACTED = "[REDACTED]"


def redact_log_text(text: str) -> str:
    """``text`` with bot tokens and credential query parameters replaced.

    Belt and braces under the httpx level below: other libraries log URLs
    too, and a record's formatted message is the last place to catch one.
    """
    return _CREDENTIAL_PARAM.sub(
        lambda m: m.group(1) + _REDACTED,
        _BOT_TOKEN.sub(lambda m: m.group(1) + _REDACTED, text),
    )


class CredentialRedactingFilter(logging.Filter):
    """Redact credentials from every record before a handler formats it.

    Rewrites the record's message in place (args folded in), so the renderer
    — JSON or console — only ever sees the redacted text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must still log
            return True
        redacted = redact_log_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


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
    handler.addFilter(CredentialRedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


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
