"""Step efficiency: fire a real repeat at the guard and see what it decides.

A positive control, and it exists because the negative one is worthless here.
Three times now this control has been read as working from a green suite and an
empty table, and three times the table was empty for a reason nobody had
tested: the counter was reset by an engine annotation, the guard was aimed at a
tool shape it never saw, and — the one this check was written for — every
decision it did reach answered a repeated read with the whole file again, which
costs exactly what the repeat would have and shows up in the evidence as a
saving.

So this check does not read a flag, count rows or import the guard and assert
about it. It makes a file in a temp directory, calls ``read_file`` three times
through ``dispatch._execute_tool`` — the same function the runner calls — with
a session registered the way a live run registers one, compacts the
conversation between the first and the second the way a long run compacts it,
and reports whether the last repeat cost less than reading the file would have.
A run of it on a box where the guard is inert says so.

It never touches the instance: its own temp directory, its own session, a guard
pinned to ``enforce`` for the duration so the answer is about the mechanism and
not about the rung this box happens to be at, and ``run_id=""`` on that guard so
nothing is written to ``agent_guardrail_events``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: Long enough that a resend and a short answer cannot be confused by size.
_BODY = "# genus doctor: step-efficiency probe\n" + ("value = 1\n" * 400)


async def _guard_answers_a_repeated_read(ctx: DoctorContext) -> Result:
    """Identical reads of an unchanged file: does a repeat cost less than a read?

    Three reads, not two, and the middle one is the point. Between the first
    and the second the probe empties the conversation of tool results, which is
    what compaction does to a long run — the state EVERY repeat in the measured
    benchmark run was decided in. The second read is then expected to carry the
    file (the model can no longer see it) and the third to be a pointer (the
    guard has just put it back). A guard that resends on the third has decided
    something, recorded a ``warned`` row for it, and saved nothing at all.
    """
    from robothor.engine import session_registry
    from robothor.engine.repeat_guard import RepeatGuard
    from robothor.engine.session import AgentSession
    from robothor.engine.tools import dispatch

    with tempfile.TemporaryDirectory(prefix="genus-doctor-repeat-") as workspace:
        target = Path(workspace) / "probe.py"
        target.write_text(_BODY, encoding="utf-8")
        args = {"path": str(target)}

        session = AgentSession(agent_id="doctor")
        session.run.tracking_disabled = True
        # Pinned here rather than resolved from the flag: the question is
        # whether the mechanism works, and `run_id=""` keeps the probe's
        # decisions out of the evidence table a sweep reads.
        session.repeat_guard = RepeatGuard(mode="enforce", session=session, run_id="")

        async def _read() -> object:
            # `owner`, because the probe is about the guard and not about how
            # this box has seeded its roles: dispatch's permission gate runs
            # first, and a denial there would read as an inert guard.
            return await dispatch._execute_tool(
                "read_file",
                dict(args),
                run_id=session.run.id,
                workspace=workspace,
                user_role="owner",
            )

        def _record(result: dict[str, object], call_id: str) -> None:
            session.record_tool_call(
                tool_name="read_file",
                tool_input=dict(args),
                tool_output=result,
                tool_call_id=call_id,
            )

        session_registry.register(session)
        try:
            first = await _read()
            if not isinstance(first, dict) or first.get("error"):
                return skip(f"the first read did not succeed: {str(first)[:120]}")
            _record(first, "doctor-1")

            # Compaction, as the run does it: the summary stays, the tool
            # results it summarised do not.
            session.messages[:] = [{"role": "user", "content": "[RETAINED CONTEXT]"}]

            second = await _read()
            if not isinstance(second, dict):
                return fail(f"the second read returned {type(second).__name__}, not a result")
            _record(second, "doctor-2")
            third = await _read()
        except Exception as exc:  # noqa: BLE001 - a broken dependency is a skip
            return skip(f"the probe could not run: {type(exc).__name__}: {exc}"[:200])
        finally:
            session_registry.unregister(session.run.id)

    if second.get("repeat_guard") != "answered":
        return fail("the second identical read ran again: the short circuit decided nothing")
    if not second.get("content"):
        return fail("the guard answered with a pointer to content the model can no longer see")
    if not isinstance(third, dict) or third.get("repeat_guard") != "answered":
        return fail("the third identical read ran again: the short circuit decided nothing")
    if third.get("content"):
        return fail(
            "every repeat is answered by resending the whole file, which costs "
            "what the repeat would have — the guard decides and saves nothing"
        )
    return ok(f"a repeated read was answered from step {third.get('unchanged_since_step')}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="step_efficiency.guard",
        title="The repeat-call guard answers a repeated read",
        category="step_efficiency",
        # A failure here costs budget, never correctness: the run still works,
        # it just pays for its repeats. Reported, never a verdict on the box.
        severity="recommended",
        run=_guard_answers_a_repeated_read,
    ),
)
