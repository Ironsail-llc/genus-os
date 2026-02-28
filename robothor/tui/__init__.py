"""
Robothor TUI — terminal chat interface for the Agent Engine.

Requires the optional `tui` dependency group:
    pip install robothor[tui]
"""

from __future__ import annotations


def check_textual() -> bool:
    """Check if Textual is installed and available."""
    try:
        import textual  # noqa: F401

        return True
    except ImportError:
        return False
