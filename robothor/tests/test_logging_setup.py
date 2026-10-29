"""A service with no logging configured gets one handler, at INFO.

The bridge never configured Python logging, so every INFO line it wrote —
including the observe rung of tenant switching — went nowhere: with no handler
on the root logger, the stdlib's last-resort handler prints WARNING and above
only. ``ensure_logging_configured`` gives such a process the engine's own
structlog setup, and leaves alone a process that already has a handler (the
engine, a test runner, a uvicorn ``log_config``).
"""

from __future__ import annotations

import logging

import pytest
import structlog


@pytest.fixture
def bare_root(monkeypatch: pytest.MonkeyPatch):
    """The root logger with no handlers, from inside the test body.

    pytest installs its capture handlers on the root logger after fixture
    setup, so they are stripped here lazily, when the test asks for the root.
    """
    root = logging.getLogger()
    saved_level = root.level
    stripped: list[logging.Handler] = []

    class _Root:
        def __call__(self) -> logging.Logger:
            stripped.extend(root.handlers)
            root.handlers.clear()
            root.setLevel(logging.WARNING)
            return root

    libraries = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield _Root()
    root.handlers[:] = stripped
    root.setLevel(saved_level)
    for name, level in libraries.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def _renderer(handler: logging.Handler) -> object:
    formatter = handler.formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    return formatter.processors[-1]


def test_a_bare_process_gets_one_handler_at_info(bare_root, monkeypatch) -> None:
    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.delenv("ROBOTHOR_LOG_FORMAT", raising=False)
    root = bare_root()
    assert ensure_logging_configured() is True
    assert len(root.handlers) == 1
    assert root.level == logging.INFO


def test_json_when_asked(bare_root, monkeypatch) -> None:
    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    root = bare_root()
    ensure_logging_configured()
    assert isinstance(_renderer(root.handlers[0]), structlog.processors.JSONRenderer)


def test_an_already_configured_process_is_left_alone(bare_root) -> None:
    from robothor.logging_setup import ensure_logging_configured

    root = bare_root()
    existing = logging.NullHandler()
    root.addHandler(existing)
    try:
        assert ensure_logging_configured() is False
        assert root.handlers == [existing]
        assert root.level == logging.WARNING
    finally:
        root.removeHandler(existing)


def test_the_engine_shares_the_one_renderer_choice() -> None:
    """One definition of json-vs-console, not a copy per entry point."""
    from robothor import logging_setup
    from robothor.engine import daemon

    assert daemon._select_log_renderer is logging_setup.select_log_renderer


# ── a configured logger must not print credentials ───────────────────
#
# httpx logs every request at INFO with the full URL. The setup wizard checks
# a Telegram token at https://api.telegram.org/bot<token>/getMe, so a root INFO
# handler would print every token an operator types into journald.

TOKEN_URL = "https://api.telegram.org/bot123456:ABC-def_GHI/getMe"


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        (f"HTTP Request: GET {TOKEN_URL} 200 OK", "ABC-def_GHI"),
        ("GET https://x.example/api?token=s3cr3t&page=2", "s3cr3t"),
        ("GET https://x.example/api?page=2&api_key=k3y", "k3y"),
        ("GET https://x.example/api?key=k3y", "k3y"),
        ("GET https://x.example/api?secret=sh", "=sh"),
        ("POST https://x.example/login?password=hunter2", "hunter2"),
        ("GET https://x.example/cb?access_token=at-123#frag", "at-123"),
    ],
)
def test_the_redactor_removes_credentials(raw: str, leaked: str) -> None:
    from robothor.logging_setup import redact_log_text

    assert leaked not in redact_log_text(raw)


def test_the_redactor_keeps_ordinary_text() -> None:
    from robothor.logging_setup import redact_log_text

    text = "GET https://x.example/api?page=2&monkey=1 200 OK; tokens=3"
    assert redact_log_text(text) == text
    assert "page=2" in redact_log_text("GET https://x.example/api?token=s&page=2")


def _render(record: logging.LogRecord, handler: logging.Handler) -> str:
    for log_filter in handler.filters:
        result = log_filter.filter(record)
        if result is False:
            return ""
        if isinstance(result, logging.LogRecord):
            record = result
    return handler.format(record)


def test_the_configured_handler_redacts_every_record(bare_root, monkeypatch) -> None:
    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    root = bare_root()
    ensure_logging_configured()
    record = logging.LogRecord(
        "httpx", logging.WARNING, __file__, 1, "HTTP Request: %s %s", ("GET", TOKEN_URL), None
    )
    rendered = _render(record, root.handlers[0])
    assert "ABC-def_GHI" not in rendered
    assert "/bot" in rendered


def test_httpx_request_lines_are_not_emitted_at_info(bare_root, monkeypatch) -> None:
    import io

    import httpx

    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    root = bare_root()
    ensure_logging_configured()
    stream = io.StringIO()
    root.handlers[0].setStream(stream)  # type: ignore[attr-defined]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        client.get(TOKEN_URL)
    assert "ABC-def_GHI" not in stream.getvalue()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
