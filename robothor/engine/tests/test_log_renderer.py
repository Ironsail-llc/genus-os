"""Production log renderer selection (daemon.py).

The dev ConsoleRenderer used to be the default unless ROBOTHOR_LOG_FORMAT was
explicitly "json" — production units never set it, so journald got 224-line
rich box-drawing tracebacks (with frame locals) per tool crash. The default is
now TTY-aware: ConsoleRenderer only when stdout is an interactive terminal,
JSON otherwise. An explicit ROBOTHOR_LOG_FORMAT always wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import structlog

from robothor.engine.daemon import _select_log_renderer

if TYPE_CHECKING:
    import pytest


def _is_console(renderer: object) -> bool:
    return isinstance(renderer, structlog.dev.ConsoleRenderer)


def _is_json(renderer: object) -> bool:
    return isinstance(renderer, structlog.processors.JSONRenderer)


def test_explicit_json_env_wins_even_on_tty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    with patch("sys.stdout.isatty", return_value=True):
        assert _is_json(_select_log_renderer())


def test_explicit_console_env_wins_even_without_tty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "console")
    with patch("sys.stdout.isatty", return_value=False):
        assert _is_console(_select_log_renderer())


def test_no_env_and_no_tty_defaults_to_json(monkeypatch: pytest.MonkeyPatch):
    """The production case: systemd unit, ROBOTHOR_LOG_FORMAT unset."""
    monkeypatch.delenv("ROBOTHOR_LOG_FORMAT", raising=False)
    with patch("sys.stdout.isatty", return_value=False):
        assert _is_json(_select_log_renderer())


def test_no_env_with_tty_keeps_console(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ROBOTHOR_LOG_FORMAT", raising=False)
    with patch("sys.stdout.isatty", return_value=True):
        assert _is_console(_select_log_renderer())
