"""The unit the wizard is made of: one step, checked before it is applied.

A step answers two questions, and the split between them is the whole design.
``check()`` says what WOULD happen and must not write anything; ``apply()``
does it. Phase 1 calls every ``check()`` and prints the result, so an operator
sees the complete plan -- and ``--yes`` refuses the whole run -- before the
first byte lands on disk.

A step that fails says so by raising :class:`StepError` with a sentence an
operator can act on. It never calls ``sys.exit`` and never prints its own
verdict: the runner owns the exit code and the renderer owns the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.init.context import InitContext

__all__ = ["ACTIONS", "BaseStep", "CheckResult", "Step", "StepError"]

#: What ``check()`` says phase 2 will do with this step.
#:
#: ``create`` -- it will run. ``exists`` -- the thing is already there, and
#: ``apply()`` is an idempotent refresh rather than a first write. ``skip`` --
#: it will not run at all (a flag turned it off, or it does not apply here).
ACTIONS = ("create", "exists", "skip")


class StepError(RuntimeError):
    """A step could not do its job. The message is shown to the operator."""


@dataclass(frozen=True)
class CheckResult:
    """What one step found, without changing anything.

    Args:
        ok: False means this step cannot succeed as things stand. For a
            required step that stops the run before anything is written.
        detail: one line, for the plan and for ``--json``.
        fix_hint: what the operator should do about a failure. Empty when
            ``ok`` -- a hint attached to a passing check is noise.
        action: one of :data:`ACTIONS`.
    """

    ok: bool
    detail: str = ""
    fix_hint: str = ""
    action: str = "create"

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, not {self.action!r}")


@runtime_checkable
class Step(Protocol):
    """One thing ``genus init`` does.

    Attributes:
        id: stable identifier. It is the key in ``init_state.yaml`` and in the
            ``--json`` payload, so it is part of the interface and does not
            change with a rename of the class.
        title: what an operator calls this step.
        required: a failing ``check()`` on a required step blocks the run.
        resumable: whether a completed run of this step may be skipped on a
            re-run. ``False`` for steps that must happen every time, such as
            the security acknowledgement.
    """

    id: str
    title: str
    required: bool
    resumable: bool

    def check(self, ctx: InitContext) -> CheckResult: ...

    def apply(self, ctx: InitContext) -> None: ...


class BaseStep:
    """Defaults for a step: required, resumable, and a check that passes.

    Subclasses override what they need. Having a base at all means a step is
    three lines when it has nothing to check, which is how the wizard stays
    readable at seventeen of them.
    """

    id: str = ""
    title: str = ""
    required: bool = True
    resumable: bool = True

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True)

    def apply(self, ctx: InitContext) -> None:
        return None
