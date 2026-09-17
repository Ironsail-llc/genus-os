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
    classify,
    remote_tokens,
    source_tokens,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LEDGER_ATTR",
    "MAX_QUOTED",
    "ObservationLedger",
    "StateChange",
    "Truncation",
    "ledger_for",
    "observe_tool_call",
]

#: The attribute the ledger hangs off the session. Lazily created, like the
#: repeat guard, so a session that never makes a tool call never builds one.
LEDGER_ATTR = "_observation_ledger"

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
