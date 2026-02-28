"""
Scratchpad — working memory injection for long-running agent sessions.

Tracks completed actions, errors, and tool call count. Periodically injects
a [WORKING STATE] summary to keep the LLM oriented during long sessions.
Mirrors Claude Code's TodoWrite pattern for orientation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Scratchpad:
    """Working memory tracker for a single agent run."""

    inject_interval: int = 3  # inject summary every N tool calls
    _tool_calls: int = 0
    _successes: int = 0
    _errors: int = 0
    _recent_actions: list[str] = field(default_factory=list)
    _recent_errors: list[str] = field(default_factory=list)
    _injected_count: int = 0

    def record_tool_call(
        self,
        tool_name: str,
        error: str | None = None,
    ) -> None:
        """Record a tool call result."""
        self._tool_calls += 1
        self._recent_actions.append(tool_name)
        if len(self._recent_actions) > 10:
            self._recent_actions = self._recent_actions[-10:]

        if error:
            self._errors += 1
            self._recent_errors.append(f"{tool_name}: {error[:100]}")
            if len(self._recent_errors) > 5:
                self._recent_errors = self._recent_errors[-5:]
        else:
            self._successes += 1

    def should_inject(self) -> bool:
        """Whether it's time to inject a working state summary."""
        if self._tool_calls == 0:
            return False
        return self._tool_calls % self.inject_interval == 0

    def format_summary(self, plan_steps: int = 0) -> str:
        """Format the current working state as a context message."""
        progress = ""
        if plan_steps > 0 and self._successes > 0:
            pct = min(100, int(self._successes / plan_steps * 100))
            progress = f"\nEstimated progress: {pct}%"

        recent = ", ".join(self._recent_actions[-5:]) or "none"
        errors = ""
        if self._recent_errors:
            errors = "\nRecent errors: " + "; ".join(self._recent_errors[-3:])

        return (
            f"[WORKING STATE]\n"
            f"Tool calls completed: {self._tool_calls} "
            f"({self._successes} ok, {self._errors} errors)\n"
            f"Recent actions: {recent}"
            f"{errors}"
            f"{progress}\n"
            f"Continue with your next action."
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for checkpoint persistence."""
        return {
            "tool_calls": self._tool_calls,
            "successes": self._successes,
            "errors": self._errors,
            "recent_actions": self._recent_actions,
            "recent_errors": self._recent_errors,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], inject_interval: int = 3) -> Scratchpad:
        """Restore from checkpoint data."""
        sp = cls(inject_interval=inject_interval)
        sp._tool_calls = data.get("tool_calls", 0)
        sp._successes = data.get("successes", 0)
        sp._errors = data.get("errors", 0)
        sp._recent_actions = data.get("recent_actions", [])
        sp._recent_errors = data.get("recent_errors", [])
        return sp
