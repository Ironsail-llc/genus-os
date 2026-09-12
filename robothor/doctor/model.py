"""What a check is, and what one may answer.

Deliberately four small dataclasses and no base class. A check is a piece of
DATA holding a coroutine, not a subclass, because the interesting half of this
package is the registry and the runner: a plugin contributes a ``Check``
instance the same way a built-in module does, and the runner cannot tell them
apart. Anything a subclass hierarchy would express here -- shared setup, a
default implementation -- belongs in :mod:`robothor.doctor.context`, which every
check receives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from robothor.doctor.context import DoctorContext

__all__ = [
    "Check",
    "FixResult",
    "Result",
    "Severity",
    "Status",
    "fail",
    "info",
    "skip",
    "ok",
]

#: How much a failure means.
#:
#: ``required``     the instance does not work; the CLI exits 1.
#: ``recommended``  the instance works but is missing something an operator
#:                  would want; reported, never fatal.
#: ``info``         reported for the record. A failure here is not a verdict.
Severity = Literal["required", "recommended", "info"]

#: ``skip`` is not a pass. A check that could not run -- no credential, no
#: systemd, Ollama not configured -- says so and names the reason, because a
#: green line for something nobody checked is how a fresh-install defect
#: survives an install gate.
Status = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class Result:
    """One check's answer.

    ``fixable`` is a property of THIS result, not of the check: ``db.migrations``
    can repair a pending migration and cannot repair a checksum drift, and the
    difference is only knowable after it has looked. The runner offers ``--fix``
    for exactly the results that say so.

    ``sub_id`` is for a check that discovers N independent conditions it could
    not know about beforehand -- ``host.unit_drift`` parses however many
    findings the host script emits. Such a check returns a LIST of results and
    each row is reported under ``<check id>:<sub_id>``, so a dashboard tracking
    one finding is not tracking a count.
    """

    status: Status
    detail: str = ""
    fixable: bool = False
    sub_id: str = ""


@dataclass(frozen=True)
class FixResult:
    """What a repair did. The runner re-runs the check afterwards regardless:
    a fix is trusted to have acted, never to have worked."""

    changed: bool
    detail: str = ""


@dataclass(frozen=True)
class Check:
    """One question the doctor asks about this instance.

    ``run`` must be a coroutine function taking the context and returning a
    :class:`Result` (or a list of them). It must not raise -- but the runner
    assumes it might, because a check whose dependency is missing is the normal
    case here and a traceback out of ``genus doctor`` on a broken box helps
    nobody.

    ``fix`` is optional and idempotent. It is called only for a result that
    declared itself ``fixable``, only under ``--fix``, and never under
    ``--dry-run``.
    """

    id: str
    title: str
    category: str
    severity: Severity
    run: Callable[[DoctorContext], Awaitable[Result | list[Result]]]
    fix: Callable[[DoctorContext], Awaitable[FixResult]] | None = None


def ok(detail: str = "") -> Result:
    """A passing result."""
    return Result(status="pass", detail=detail)


def fail(detail: str, *, fixable: bool = False, sub_id: str = "") -> Result:
    """A failing result. ``detail`` is what the operator acts on, so it names
    the thing and the repair -- never a value, and never instruction text out
    of a manifest."""
    return Result(status="fail", detail=detail, fixable=fixable, sub_id=sub_id)


def skip(detail: str) -> Result:
    """A check that could not run, and why. Never a silent pass."""
    return Result(status="skip", detail=detail)


def info(detail: str) -> Result:
    """Something worth recording that is not a verdict; reported as a pass."""
    return Result(status="pass", detail=detail)
