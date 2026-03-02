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
    max_injections: int = 5  # stop injecting into messages after this many
    _tool_calls: int = 0
    _successes: int = 0
    _errors: int = 0
    _recent_actions: list[str] = field(default_factory=list)
    _recent_errors: list[str] = field(default_factory=list)
    _injected_count: int = 0

    # Plan-aware progress tracking
    _plan_steps: list[dict[str, Any]] = field(default_factory=list)
    _current_step: int = 0
    _step_attempts: dict[int, int] = field(default_factory=dict)
    _completed_steps: set[int] = field(default_factory=set)

    def set_plan(self, plan_steps: list[dict[str, Any]]) -> None:
        """Store plan steps for progress tracking.

        Each step should have at minimum 'step' (int) and optionally
        'tool' (str) and 'action' (str) fields.
        """
        self._plan_steps = list(plan_steps)
        self._current_step = 0
        self._step_attempts = {}
        self._completed_steps = set()

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
            # Track failures on current plan step
            if self._plan_steps and self._current_step < len(self._plan_steps):
                self._step_attempts[self._current_step] = (
                    self._step_attempts.get(self._current_step, 0) + 1
                )
        else:
            self._successes += 1
            # Try to advance plan progress on success
            if self._plan_steps:
                self._try_advance_step(tool_name)

    def _try_advance_step(self, tool_name: str) -> None:
        """Heuristic: if the tool matches the current plan step's tool, advance."""
        if self._current_step >= len(self._plan_steps):
            return

        step = self._plan_steps[self._current_step]
        step_tool = step.get("tool", "")

        # Match current step's tool
        if step_tool and step_tool == tool_name:
            self._completed_steps.add(self._current_step)
            self._current_step += 1
            return

        # If no tool on current step or tool doesn't match, check if any
        # upcoming step matches (allows for out-of-order execution)
        for i in range(self._current_step, len(self._plan_steps)):
            if i in self._completed_steps:
                continue
            s_tool = self._plan_steps[i].get("tool", "")
            if s_tool and s_tool == tool_name:
                self._completed_steps.add(i)
                # Advance current_step past any completed steps
                while (
                    self._current_step < len(self._plan_steps)
                    and self._current_step in self._completed_steps
                ):
                    self._current_step += 1
                return

        # No match — if current step has no tool, advance on any success
        if not step_tool:
            self._completed_steps.add(self._current_step)
            self._current_step += 1

    def should_inject(self) -> bool:
        """Whether it's time to inject a working state summary.

        Returns False once max_injections is reached to prevent unbounded
        context growth. Tracking continues internally regardless.
        """
        if self._tool_calls == 0:
            return False
        if self._injected_count >= self.max_injections:
            return False
        return self._tool_calls % self.inject_interval == 0

    @property
    def steps_completed(self) -> int:
        """Number of plan steps completed."""
        return len(self._completed_steps)

    @property
    def total_plan_steps(self) -> int:
        """Total number of plan steps."""
        return len(self._plan_steps)

    @property
    def current_step_attempts(self) -> int:
        """Number of failed attempts on the current plan step."""
        return self._step_attempts.get(self._current_step, 0)

    def format_summary(self, plan_steps: int = 0) -> str:
        """Format the current working state as a context message."""
        self._injected_count += 1

        # Plan-aware progress (preferred over legacy plan_steps param)
        progress = ""
        if self._plan_steps:
            total = len(self._plan_steps)
            completed = len(self._completed_steps)
            if self._current_step < total:
                step = self._plan_steps[self._current_step]
                step_tool = step.get("tool", "?")
                step_action = step.get("action", "")
                attempts = self._step_attempts.get(self._current_step, 0)
                if attempts >= 3:
                    progress = (
                        f"\nPlan progress: Stuck on step "
                        f"{self._current_step + 1}/{total} ({step_tool}) "
                        f"— {attempts} failed attempts"
                    )
                else:
                    desc = f" — {step_action}" if step_action else ""
                    progress = (
                        f"\nPlan progress: Step "
                        f"{self._current_step + 1}/{total} ({step_tool}){desc} "
                        f"— on track"
                    )
            else:
                progress = f"\nPlan progress: {completed}/{total} complete"
        elif plan_steps > 0 and self._successes > 0:
            # Legacy fallback: estimate from success ratio
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
            "plan_steps": self._plan_steps,
            "current_step": self._current_step,
            "step_attempts": {str(k): v for k, v in self._step_attempts.items()},
            "completed_steps": list(self._completed_steps),
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
        sp._plan_steps = data.get("plan_steps", [])
        sp._current_step = data.get("current_step", 0)
        raw_attempts = data.get("step_attempts", {})
        sp._step_attempts = {int(k): v for k, v in raw_attempts.items()}
        sp._completed_steps = set(data.get("completed_steps", []))
        return sp
