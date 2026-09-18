"""What the run is TOLD about its own unfinished observations.

The other half of `observation_ledger`, split from it because they answer
different questions and only one of them is allowed to change a run.
`observation_ledger` knows what happened: which results were cut, which calls
changed something, what has been read since. This module decides what, if
anything, the model hears about it — and when.

"When" is the part that was wrong twice, and both times in the same direction:

* the honest-completion note was appended AFTER the runner had already decided
  to return, so it reached the transcript and nothing else (hostile review C1).
  The runner's stop branch is ``if nudge(...): continue`` / ``return``, and
  ``get_final_text`` walks back past anything appended after the last assistant
  message — so a note that does not come with a True is a note nobody reads;
* act→observe was delivered only from `loop_guards.append_engine_note`, whose
  call sites are the deadline rungs and a check-in whose cadence is 25
  iterations. The run this control exists for made 21 requests in 85.9 s of a
  300 s budget and crossed neither (I2).

So both live here now, both delivered at the moment a run tries to stop, and
both bounded: two model turns for an unresolved truncation, one for an
unobserved change, and then the run ends whatever the ledger still says. The
failure being corrected is a confident wrong answer; a stuck run is not an
improvement on it.

**What `observe` means here.** Nothing reaches the model. The rung logs what
`enforce` would have done and writes a guardrail row, and the run ends exactly
as it would have — this repo's rule for an observe rung, and what lets a sweep
comparing the rungs measure the control rather than the wording.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from robothor.engine.act_observe import act_observe_note
from robothor.engine.observation_ledger import LEDGER_ATTR, MAX_QUOTED, ledger_for

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_HOLDS",
    "observation_note_parts",
    "observation_notes",
    "record_observation_verdicts",
    "unobserved_change_nudge",
    "unread_observation_hold",
]

#: How many model turns an unresolved truncation is worth. TWO, and each one is
#: a different sentence: "go and read it", then — if the run still tries to stop
#: with the entry outstanding — "say in your answer what you did not read". Both
#: have to be real holds; see the module header for why a False is inert.
MAX_HOLDS = 2


def observation_notes(session: Any) -> str:
    """The observation notes due now, joined — or "" when there is nothing.

    Kept for callers that want one string; the runner's path is
    :func:`observation_note_parts`, which keeps each note its own message.
    """
    return "\n".join(observation_note_parts(session))


def observation_note_parts(session: Any) -> list[str]:
    """What the model is due to hear at the next engine note, one item each.

    Called from ``loop_guards.append_engine_note``, so every deliverable
    check-in and every deadline rung carries them. Each truncation is quoted at
    most once and the act→observe sentence at most once per run, because a
    control that repeats itself is a control that gets skimmed.

    A LIST, not a string, because of what the measured run was handed: one
    ``[SYSTEM]`` message that opened "Decide NOW what the smallest complete
    deliverable is" and, three sentences in, said "read it again before you
    write your answer". Two instructions in one message is one instruction,
    and it was the first — the model went straight to ``write_file``.
    """
    from robothor.engine.feature_flags import act_observe_mode, truncation_ledger_mode

    ledger = ledger_for(session)
    if ledger is None:
        return []
    parts: list[str] = []

    truncation_mode = truncation_ledger_mode()
    if truncation_mode != "off":
        fresh = ledger.take_unquoted() if truncation_mode == "enforce" else ledger.unresolved()
        if fresh and truncation_mode == "enforce":
            parts.append(
                "[SYSTEM] Unfinished observations:\n"
                + "\n".join(f"- {entry.sentence()}" for entry in fresh)
            )
        elif fresh:
            logger.warning(
                "truncation ledger observe: run %s would quote %d unresolved truncation(s): %s",
                _run_id(session),
                len(fresh),
                "; ".join(entry.sentence() for entry in fresh[:MAX_QUOTED]),
            )

    act_mode = act_observe_mode()
    if act_mode != "off" and not ledger.change_note_given:
        pending = ledger.unobserved_changes()
        note = act_observe_note(pending)
        if note and act_mode == "enforce":
            ledger.change_note_given = True
            parts.append(note)
        elif note:
            logger.warning(
                "act-observe observe: run %s would be told about %d unobserved change(s): %s",
                _run_id(session),
                len(pending),
                note.replace("\n", " ")[:300],
            )
    return parts


def unread_observation_hold(session: Any) -> bool:
    """The run wants to stop. Does it still owe itself a read?

    True means "do not end this iteration": a note has been appended and the
    loop runs another model turn with it in context. Only at ``enforce``, at
    most :data:`MAX_HOLDS` times, and only for a truncation the run could still
    resolve.

    Two holds, and the second one is the point. Hold 1 says "go and read it".
    Hold 2 — spent when the run tries to stop again with the entry STILL
    unresolved — says "then say so in your answer", and it must return True as
    well, because of how the runner's stop branch is written::

        if nudge_for_missing_deliverable(session, _workspace):
            continue
        return

    A False there returns immediately, no further LLM call happens, and
    ``session.get_final_text`` walks back to the last message whose role is
    ``assistant`` — which is the answer produced BEFORE the note. The first cut
    of this function appended the honest-completion text and returned False, so
    the note reached nothing but the transcript and the run's output was
    byte-identical to what ``observe`` would have produced. A control that
    appends a string to itself and calls it an honest completion is
    `controls-were-armed-but-aimed-at-nothing` with a better name.

    After both holds the run ends whatever the ledger still says. The failure
    being corrected is a confident wrong answer; a stuck run is not an
    improvement on it.
    """
    from robothor.engine.feature_flags import truncation_ledger_mode
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    mode = truncation_ledger_mode()
    if mode == "off":
        return False
    ledger = ledger_for(session)
    if ledger is None:
        return False
    outstanding = ledger.unresolved()
    if not outstanding:
        return False
    if mode != "enforce":
        logger.warning(
            "truncation ledger observe: run %s would be held for %d unread observation(s)",
            _run_id(session),
            len(outstanding),
        )
        return False
    if ledger.holds_used >= MAX_HOLDS:
        logger.warning(
            "truncation ledger enforce: run %s ends with %d unread observation(s)",
            _run_id(session),
            len(outstanding),
        )
        return False
    quoted = "\n".join(f"- {entry.sentence()}" for entry in outstanding[:MAX_QUOTED])
    if ledger.holds_used == MAX_HOLDS - 1:
        body = (
            "[SYSTEM] You are about to finish with these observations still unread:\n"
            f"{quoted}\n"
            "Give your final answer now, and say in it what you did not read and what it "
            "would have changed. Do not present the answer as complete."
        )
    else:
        body = (
            "[SYSTEM] Before you finish: part of what you asked for was never shown to you.\n"
            f"{quoted}\n"
            "Read it now if your answer depends on it. If it does not, say why in your "
            "answer and finish."
        )
    ledger.holds_used += 1
    session.messages.append(
        {
            "role": ENGINE_CONTEXT_ROLE,
            "content": body,
        }
    )
    logger.warning(
        "truncation ledger enforce: run %s held (%d/%d) for %d unread observation(s)",
        _run_id(session),
        ledger.holds_used,
        MAX_HOLDS,
        len(outstanding),
    )
    return True


def unobserved_change_nudge(session: Any) -> bool:
    """The run wants to stop having changed something it never looked at again.

    True means "do not end this iteration" — one more model turn with the note
    in context, at most ONCE per run, and only at ``enforce``.

    It is delivered here rather than only at a check-in because the check-in
    cadence is every 25 iterations on the step-efficiency ``enforce`` rung, and
    the deadline rungs are at 50/80/95% of the budget. The run this control was
    built for made 21 requests in 85.9 s of a 300 s budget and would have
    crossed neither (hostile review I2) — the one moment it was certain to
    reach is the moment it tried to stop.

    One turn, not a loop. The flag's promise is that it never FAILS a run, and
    it does not: the run always completes, and the worst case is one extra
    model call. Appending the note and returning False would have been the
    cheaper-looking option and it is the one C1 records — the runner returns on
    a False and ``get_final_text`` walks back past anything appended after the
    last assistant message, so the note would reach the transcript and nothing
    else.

    Regardless of whether the mid-run note was shown. MEASURED 2026-09-17: the
    note was delivered once, folded into the 50 % deadline blob, the model went
    straight to ``write_file``, and this function — then gated on
    ``change_note_given`` — said nothing at the stop. A note is a nudge; the
    hold is the guarantee, and they are latched separately.
    """
    from robothor.engine.feature_flags import act_observe_mode
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    mode = act_observe_mode()
    if mode == "off":
        return False
    ledger = ledger_for(session)
    if ledger is None or ledger.change_hold_used:
        return False
    pending = ledger.unobserved_changes()
    note = act_observe_note(pending)
    if not note:
        return False
    if mode != "enforce":
        logger.warning(
            "act-observe observe: run %s would be told at its stop about %d unobserved "
            "change(s): %s",
            _run_id(session),
            len(pending),
            note.replace("\n", " ")[:300],
        )
        return False
    ledger.change_hold_used = True
    ledger.change_note_given = True
    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": note})
    logger.warning(
        "act-observe enforce: run %s told at its stop about %d unobserved change(s)",
        _run_id(session),
        len(pending),
    )
    return True


def record_observation_verdicts(run: Any, session: Any, workspace: Any = None) -> None:
    """The end-of-run record: what this run never read, and what it never re-read.

    A guardrail row rather than a log line, because the evidence query an
    operator runs to decide whether to promote this flag has to be able to
    count it — a control whose only trace is a message it printed to itself is
    the shape `feedback-probe-dont-trust-silence` records.
    """
    from robothor.engine.feature_flags import act_observe_mode, truncation_ledger_mode

    # The spill files this run wrote go with it. They exist so the agent can
    # page its own truncated output back WHILE the run is alive; nothing reads
    # them afterwards, and a directory nobody looks at that only ever grows is
    # how the `analyze_image` spill shipped its one bad release. A run killed
    # before it reaches here leaves orphans, which is what the retention sweep
    # in `retention.run_retention_cleanup` is the backstop for.
    ledger = getattr(session, LEDGER_ATTR, None)
    with contextlib.suppress(Exception):
        from robothor.engine.exec_spill import prune_run_spills

        run_id = getattr(run, "id", "") or ""
        # Every workspace this run's spills were written under, not just the
        # engine's. An agent whose manifest names its own workspace — and every
        # sandboxed or benchmark run — writes its spills there, and reaping
        # only the workspace this function was handed left them to the 7-day
        # sweep, which looks in the instance workspace and nowhere else.
        roots = list(ledger.spill_workspaces()) if ledger is not None else []
        if workspace and str(workspace) not in roots:
            roots.append(str(workspace))
        for root in roots:
            prune_run_spills(root, run_id)

    # The third ladder's row, ABOVE the ledger guard below and not after it:
    # it sat after, so the verdict control wrote nothing whenever the OTHER two
    # flags were both off — no ledger is built then, the guard returned, and a
    # flag reported a permanent zero because of somebody else's setting
    # (hostile review, Critical 2). A failed write is logged, never swallowed.
    try:
        from robothor.engine.verdict_commitment import record_verdict_findings

        record_verdict_findings(run, session, workspace)
    except Exception:
        row_run = getattr(run, "id", "?")
        logger.warning("verdict commitment: run %s wrote no guardrail row", row_run, exc_info=True)

    if ledger is None:
        return
    truncation_mode = truncation_ledger_mode()
    if truncation_mode != "off":
        outstanding = ledger.unresolved()
        if outstanding:
            _log_event(
                run,
                "truncation_ledger",
                truncation_mode,
                f"{len(outstanding)} truncated tool result(s) were never read back: "
                + "; ".join(entry.sentence() for entry in outstanding[:MAX_QUOTED]),
            )
    act_mode = act_observe_mode()
    if act_mode != "off":
        pending = ledger.unobserved_changes()
        if pending:
            _log_event(
                run,
                "act_observe",
                act_mode,
                act_observe_note(pending),
            )


#: Controls that can HOLD a run at `enforce`, and so may honestly write a
#: `blocked` guardrail row. `act_observe` is deliberately absent: its own flag
#: doc promises it never fails a run, and a table reading three blocks where one
#: is advice is a table an operator cannot act on (hostile review M1).
_CAN_BLOCK = frozenset({"truncation_ledger", "verdict_commitment"})


def _log_event(run: Any, name: str, mode: str, reason: str) -> None:
    with contextlib.suppress(Exception):
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run.id,
            guardrail_name=name,
            action="blocked" if (mode == "enforce" and name in _CAN_BLOCK) else "observed",
            reason=reason[:500],
            mode=mode,
        )


def _run_id(session: Any) -> str:
    return str(getattr(getattr(session, "run", None), "id", "") or "?")
