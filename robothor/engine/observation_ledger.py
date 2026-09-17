"""What this run has not finished reading, and what it changed since it looked.

Two ledgers, one hook. Both answer a question the engine could not previously
ask at the end of a run: *is the answer about to be written based on everything
the run actually saw?*

**Truncations.** A tool result that says it was cut is a KNOWN UNKNOWN. #583
taught ``exec`` to say so and `exec_spill` taught it to keep the rest, but
nothing checked whether the agent went and got it — and on the measured run it
did not. Each truncated result registers ``(step, tool, chars_shown,
chars_total, path)``; the entry clears when the run reads that spill back, or
re-runs a narrower command over the same source and gets a whole answer this
time. It does NOT clear because the model mentioned the path: the failure being
fixed is a confident answer over partial input, and an agent that says "some
output was truncated" in its report has demonstrated nothing except that it
read the marker.

**State changes.** A call that changed something at the other end invalidates
every observation of that source that preceded it. `act_observe` does the
classification; this module keeps the per-run tally and answers "which changes
has nothing read since".

**The ladder, and what `observe` means here.** Both controls are gated, and at
``observe`` neither puts a word in front of the model — it logs what ``enforce``
would have done, and the run ends exactly as it would have. That is this repo's
rule for an observe rung (`reask_for_wrong_deliverable_shape`, `step_efficiency`
`off`), and it is what lets a sweep comparing the rungs measure the control
rather than the wording. At ``enforce`` the unresolved entries are quoted to the
model, once each, and a truncation still unresolved holds the run for exactly
ONE more ask before it completes honestly saying what it never read. Never a
loop: the failure here is a confident wrong answer, and a stuck run is not an
improvement on it.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from robothor.engine.act_observe import (
    CHANGE,
    READ,
    act_observe_note,
    classify,
    remote_tokens,
    source_tokens,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ObservationLedger",
    "StateChange",
    "Truncation",
    "ledger_for",
    "observation_notes",
    "observe_tool_call",
    "record_observation_verdicts",
    "unread_observation_hold",
]

#: The attribute the ledger hangs off the session. Lazily created, like the
#: repeat guard, so a session that never makes a tool call never builds one.
LEDGER_ATTR = "_observation_ledger"

#: How many model turns an unresolved truncation is worth. TWO, and each one
#: is a different sentence: "go and read it", then — if the run still tries to
#: stop with the entry outstanding — "then say in your answer what you did not
#: read". Both have to be real holds, because the runner's stop branch returns
#: on a False and `get_final_text` then walks back past anything appended after
#: the last assistant message. Two and no more: an unbounded "you are not done"
#: is a loop, and a run whose entry can never be resolved — the spill gone, the
#: source gone — has to be able to finish.
MAX_HOLDS = 2

#: How many entries a single note quotes. Beyond this the note stops being read.
MAX_QUOTED = 3


@dataclass(frozen=True)
class Truncation:
    """One tool result the model saw only part of."""

    step: int
    tool: str
    chars_shown: int
    chars_total: int
    path: str
    sources: frozenset[str] = frozenset()

    def sentence(self) -> str:
        where = (
            f"the rest is at `{self.path}`"
            if self.path
            else "the rest was not written anywhere, so re-run it narrower"
        )
        return (
            f"step {self.step}'s `{self.tool}` output was cut: {self.chars_shown:,} of "
            f"{self.chars_total:,} chars shown; {where}. If your answer depends on it, "
            "read it before you finish."
        )


@dataclass(frozen=True)
class StateChange:
    """One call that changed something at the other end."""

    step: int
    tool: str
    sources: frozenset[str]


@dataclass
class ObservationLedger:
    """Per-run, in memory, and deliberately small.

    It holds step numbers and a handful of tokens per call — never a payload.
    The payloads are on the step rows and, for a truncation, in the spill file;
    a second copy in RAM for the length of a run is what
    ``_offloaded_paths`` was careful not to become.
    """

    truncations: list[Truncation] = field(default_factory=list)
    changes: list[StateChange] = field(default_factory=list)
    resolved: set[int] = field(default_factory=set)
    quoted: set[int] = field(default_factory=set)
    #: Step numbers of reads, with what they read, newest last.
    reads: list[tuple[int, frozenset[str]]] = field(default_factory=list)
    change_note_given: bool = False
    holds_used: int = 0

    # ── recording ───────────────────────────────────────────────────────

    def record(self, step: int, tool: str, args: dict[str, Any], output: Any) -> None:
        """One admitted call, after it ran. Never raises.

        A call that FAILED is not an observation and not a change. A guardrail
        refusal, a timeout or a connection error leaves the world exactly as it
        was — counting a blocked `send_email` as "you changed something and have
        not looked since" would fire the note on a run that did nothing, which
        is the false positive that gets a control ignored.
        """
        if isinstance(output, dict) and output.get("error"):
            return
        sources = source_tokens(args)
        kind = classify(tool, args, _read_only())
        if kind == READ:
            self.reads.append((step, sources))
            self._resolve_by_read(step, tool, args, sources, output)
        elif kind == CHANGE:
            # The REMOTE ones only. A change is recorded so the note can say
            # "you have not read THAT source since", and the answer has to be
            # somewhere the agent could go and look — not the run's own
            # scratch paths, which reading again would tell it nothing.
            self.changes.append(StateChange(step, tool, remote_tokens(args)))
        self._register_truncations(step, tool, sources, output)

    def _register_truncations(
        self, step: int, tool: str, sources: frozenset[str], output: Any
    ) -> None:
        if not isinstance(output, dict):
            return
        for stream in ("stdout", "stderr"):
            if not output.get(f"{stream}_truncated"):
                continue
            total = int(output.get(f"{stream}_chars") or 0)
            shown = max(0, total - 1)
            text = output.get(stream)
            if isinstance(text, str):
                shown = len(text)
            self.truncations.append(
                Truncation(
                    step=step,
                    tool=tool,
                    chars_shown=min(shown, total) if total else shown,
                    chars_total=total or shown,
                    path=str(output.get(f"{stream}_path") or ""),
                    sources=sources,
                )
            )

    def _resolve_by_read(
        self,
        step: int,
        tool: str,
        args: dict[str, Any],
        sources: frozenset[str],
        output: Any,
    ) -> None:
        """Clear the entries this read actually answered.

        Two ways, and BOTH require a tool call that returned something whole:

        * the run read the spill file back — the path appears in this call's
          arguments;
        * the run asked the same source again, narrower, and this time nothing
          was cut.
        """
        argument_text = " ".join(str(v) for v in (args or {}).values())
        truncated_now = isinstance(output, dict) and (
            output.get("stdout_truncated") or output.get("stderr_truncated")
        )
        for entry in self.truncations:
            if entry.step in self.resolved:
                continue
            if entry.path and entry.path in argument_text:
                self.resolved.add(entry.step)
                continue
            if truncated_now or entry.tool != tool:
                continue
            if entry.sources and sources & entry.sources:
                self.resolved.add(entry.step)

    # ── reading back ────────────────────────────────────────────────────

    def unresolved(self) -> list[Truncation]:
        return [t for t in self.truncations if t.step not in self.resolved]

    def unobserved_changes(self) -> list[tuple[int, str, frozenset[str]]]:
        """Changes nothing has read since.

        A read with no source in common is not an answer to a change against a
        named source — "I read a file" does not make an inbox current. A change
        that named no source is answered by any later read, because there is
        nothing more specific to ask for.
        """
        out: list[tuple[int, str, frozenset[str]]] = []
        for change in self.changes:
            later = [(s, src) for s, src in self.reads if s > change.step]
            if not later:
                out.append((change.step, change.tool, change.sources))
                continue
            if change.sources and not any(src & change.sources for _s, src in later):
                out.append((change.step, change.tool, change.sources))
        return out

    def take_unquoted(self) -> list[Truncation]:
        """Unresolved entries this run has not been told about yet."""
        fresh = [t for t in self.unresolved() if t.step not in self.quoted][:MAX_QUOTED]
        self.quoted.update(t.step for t in fresh)
        return fresh


def _read_only() -> frozenset[str]:
    try:
        from robothor.engine.tools.read_only import declared_read_only_tools

        return declared_read_only_tools()
    except Exception:  # noqa: BLE001 - an unavailable classification classifies nothing
        return frozenset()


def ledger_for(session: Any) -> ObservationLedger | None:
    """This run's ledger, created on first use. None when there is no session."""
    if session is None:
        return None
    ledger = getattr(session, LEDGER_ATTR, None)
    if ledger is None:
        ledger = ObservationLedger()
        with contextlib.suppress(AttributeError):
            setattr(session, LEDGER_ATTR, ledger)
    return ledger


def observe_tool_call(session: Any, step: Any) -> None:
    """Record one finished tool call on the run's ledger. Never raises.

    Hooked at ``session.record_tool_call``, which is the ONE place every
    admitted call passes — a turn's call and a snippet's proxied call alike.
    Hooking the turn loop instead would have missed the proxied ones, which is
    exactly where the measured run threw its evidence away.
    """
    if _both_modes_off():
        return
    try:
        ledger = ledger_for(session)
        if ledger is None:
            return
        ledger.record(
            int(getattr(step, "step_number", 0) or 0),
            str(getattr(step, "tool_name", "") or ""),
            dict(getattr(step, "tool_input", None) or {}),
            getattr(step, "tool_output", None),
        )
    except Exception as exc:  # noqa: BLE001 - a ledger never breaks tool recording
        logger.debug("observation ledger skipped a step: %s", exc)


def _both_modes_off() -> bool:
    try:
        from robothor.engine.feature_flags import act_observe_mode, truncation_ledger_mode

        return truncation_ledger_mode() == "off" and act_observe_mode() == "off"
    except Exception:  # noqa: BLE001 - an unreadable flag is not a reason to crash
        return False


def observation_notes(session: Any) -> str:
    """What to add to the next engine note, or "" when there is nothing.

    Called from ``loop_guards.append_engine_note``, so every deliverable
    check-in and every deadline rung carries it. Each truncation is quoted at
    most once and the act→observe sentence at most once per run, because a
    control that repeats itself is a control that gets skimmed.
    """
    from robothor.engine.feature_flags import act_observe_mode, truncation_ledger_mode

    ledger = ledger_for(session)
    if ledger is None:
        return ""
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
    return "\n".join(parts)


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
    with contextlib.suppress(Exception):
        from robothor.engine.exec_spill import prune_run_spills

        prune_run_spills(workspace, getattr(run, "id", "") or "")

    ledger = getattr(session, LEDGER_ATTR, None)
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
    # The third ladder's row, written here rather than beside its own check so
    # the finalizer has ONE call site for the observation cluster. Without it
    # `flags/evidence.py` would report the verdict control permanently inert —
    # the failure mode this repo keeps rediscovering.
    with contextlib.suppress(Exception):
        from robothor.engine.verdict_commitment import record_verdict_findings

        record_verdict_findings(run, session, workspace)


def _log_event(run: Any, name: str, mode: str, reason: str) -> None:
    with contextlib.suppress(Exception):
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run.id,
            guardrail_name=name,
            action="blocked" if mode == "enforce" else "observed",
            reason=reason[:500],
            mode=mode,
        )


def _run_id(session: Any) -> str:
    return str(getattr(getattr(session, "run", None), "id", "") or "?")
