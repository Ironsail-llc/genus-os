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
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    yield root
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _renderer(handler: logging.Handler) -> object:
    formatter = handler.formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    return formatter.processors[-1]


def test_a_bare_process_gets_one_handler_at_info(bare_root, monkeypatch) -> None:
    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.delenv("ROBOTHOR_LOG_FORMAT", raising=False)
    assert ensure_logging_configured() is True
    assert len(bare_root.handlers) == 1
    assert bare_root.level == logging.INFO


def test_json_when_asked(bare_root, monkeypatch) -> None:
    from robothor.logging_setup import ensure_logging_configured

    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    ensure_logging_configured()
    assert isinstance(_renderer(bare_root.handlers[0]), structlog.processors.JSONRenderer)


def test_an_already_configured_process_is_left_alone(bare_root) -> None:
    from robothor.logging_setup import ensure_logging_configured

    existing = logging.NullHandler()
    bare_root.addHandler(existing)
    assert ensure_logging_configured() is False
    assert bare_root.handlers == [existing]
    assert bare_root.level == logging.WARNING


def test_the_engine_shares_the_one_renderer_choice() -> None:
    """One definition of json-vs-console, not a copy per entry point."""
    from robothor import logging_setup
    from robothor.engine import daemon

    assert daemon._select_log_renderer is logging_setup.select_log_renderer
