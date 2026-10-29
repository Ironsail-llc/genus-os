"""Where code outside the daemon finds the running ``WorkflowEngine``.

The daemon constructs one engine and hands it to the scheduler, hooks and the
health API by argument. Agent tool handlers have no such argument — they are
dispatched by name with only a ``ToolContext`` — so the daemon also registers
the engine here, the same shape as ``leader``'s elector accessors. ``None``
means no engine is running in this process (CLI, tests, a worker without
workflows), and callers must say so rather than guess.
"""

from __future__ import annotations

from typing import Any

_engine: Any = None


def get_workflow_engine() -> Any:
    """The process's workflow engine, or None when none was registered."""
    return _engine


def set_workflow_engine(engine: Any) -> None:
    """Register (or clear, with None) the process's workflow engine."""
    global _engine
    _engine = engine
