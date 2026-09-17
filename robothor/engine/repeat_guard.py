"""A call already made, answered the same way, is not a step.

Profiled from the bench pod's ``agent_run_steps`` for the `sam3_debug` task
(2026-09-15, killed at its 1200s ceiling with nothing written): the SAME
``exec`` — one ``python3 …`` invocation, requested timeout 900 — ran NINE
times; the same ``read_file`` of one module ran six times, and four other files
two to four times each. 86 tool calls consumed 435s of the budget, and the last
thing the run did before the circuit breaker killed it was another
``read_file``.

Nothing in the engine could act on that. ``Scratchpad``'s no-progress detector
notices a repeated (tool, args, result) triple and puts a line in its summary,
which is narration rather than a decision and arrives after the repeats have
been paid for. ``dedup.py`` is cross-RUN agent dedup and unrelated.

Three rules. The first two are narrow, and the narrowness is the design; the
third is deliberately coarse, because the narrowness is what a loop walks
around — see ``repeat_variants`` for the shapes that did.

* **Reads.** ``read_file`` and ``list_directory`` name a single stat-able
  target, so an identical call whose target has the same mtime and size as at
  the previous read can be answered from what the run already has. It is
  answered SHORT only while the earlier result is still in the conversation; if
  compaction or thinning removed it, the full content comes back. A guard that
  leaves the model unable to see a file it is holding a pointer to is worse than
  the repeat it prevented. A resend is itself a read, so the guard records what
  it put back — without that the next repeat looked for a payload no message
  held any more, resent the file again, and went on doing so for the rest of the
  run. ``search_files`` has no single target to stat, so it is counted by its
  output like ``exec`` and only ever noted.

* **exec.** A command may have side effects the engine cannot see, so the third
  identical-output occurrence gets a note and STILL RUNS. Only after four
  byte-identical outputs is a fifth refused — at that point the command has
  demonstrated it is not changing, and the refusal says exactly what to do:
  change something. Any change to the arguments is a different key and runs
  immediately, so nothing is trapped.

* **Varying arguments.** ``robothor/engine/repeat_variants.py``, wired in at
  ``_decide_variant`` and ``after``. Everything above keys on a canonical hash
  of the arguments, which a loop escapes by changing one flag, one offset or
  one word of a query. That guard groups calls by a coarse signature and
  escalates only while their RESULTS stop carrying anything new — and an exact
  decision here spends its streak, so the escape hatch above ("any change to
  the arguments runs immediately") is never closed by both at once.

Everything else — ``write_file``, ``view_image``, every tool named in neither
list — is untouched.

Observe logs what enforce would have done, at WARNING. That level is not a
style choice: the benchmark container installs no logging configuration, so
Python's ``lastResort`` handler drops everything below WARNING, and the
pre-existing deadline note spent a whole profiling day looking inert for
exactly that reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robothor.engine.repeat_variants import VARIANT_TOOLS, VariantTracker

logger = logging.getLogger(__name__)

#: Reads that name one target the filesystem can be asked about AND whose whole
#: answer is that target's bytes. An identical call on an unchanged target is
#: answerable without running the tool.
#:
#: ``read_file`` and nothing else. ``list_directory`` was here and was wrong:
#: the handler returns every entry's SIZE, and with ``recursive: true`` the whole
#: subtree, while the fingerprint stats only the directory — whose mtime does not
#: move when a file inside it grows, and does not move at all when a file appears
#: two levels down. The guard therefore said "the content is unchanged on disk"
#: about a listing that was out of date, and the loop it broke is precisely "did
#: my deliverable land, and how big is it" — the question the rest of this module
#: exists to encourage. A listing can only be trusted by taking it again, so it
#: is output-counted below and costs the call it would have saved.
SHORT_CIRCUIT_TOOLS: frozenset[str] = frozenset({"read_file"})

#: Calls with no single stat-able answer, counted by whether their OUTPUT
#: changed. ``search_files`` walks a tree, ``list_directory`` reports sizes it
#: does not stat, and ``exec`` can do anything.
OUTPUT_COUNTED_TOOLS: frozenset[str] = frozenset({"exec", "search_files", "list_directory"})

#: Everything the guard may look at. An allow-list, never a deny-list: a new
#: tool is unguarded until someone decides it is safe to guard, which is the
#: right default for a control that can withhold a call.
GUARDED_TOOLS: frozenset[str] = SHORT_CIRCUIT_TOOLS | OUTPUT_COUNTED_TOOLS

#: The only tool a refusal may ever apply to. A search is cheap; refusing one
#: buys a second and costs a capability.
REFUSABLE_TOOLS: frozenset[str] = frozenset({"exec"})

#: Every tool this guard looks at at all — the exact rules above plus the
#: varying-argument families in ``repeat_variants``. The variant set is wider
#: (``web_search``, ``web_fetch``): a reworded search is one of the three
#: recorded loop shapes, and it has no exact rule to be caught by.
_WATCHED_TOOLS: frozenset[str] = GUARDED_TOOLS | VARIANT_TOOLS

#: Occurrence at which an identical-output call is noted (and still runs).
NOTE_AT: int = 3

#: Occurrence at which an identical-output ``exec`` is refused. Four prior runs
#: with byte-identical output is the evidence; the fifth is the one refused.
REFUSE_AT: int = 5

#: Results larger than this are not remembered. The guard may have to resend a
#: result it withheld, so it must hold what it tracked — and holding megabytes
#: per key to maybe resend them is the wrong trade. An untracked read simply
#: runs again, which is the safe direction.
MAX_TRACKED_CHARS: int = 200_000


def canonical_key(tool_name: str, args: dict[str, Any] | None) -> str:
    """``(tool, canonical JSON of args)`` as one hashable string.

    Sorted keys so argument order cannot make one call look like two, and
    ``default=str`` so an unserialisable argument degrades to a stable string
    rather than raising inside a guard that must never break a tool call.
    """
    try:
        body = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers these
        body = repr(args)
    return f"{tool_name}\x00{body}"


#: The fields of a result that are the TOOL's answer, per output-counted tool.
#: An allow-list, because the alternative is a deny-list that has to be extended
#: every time a control learns to annotate a result — and the first one to do so
#: silently disarmed this guard.
#:
#: ``error`` is in every entry on purpose: a command that fails identically five
#: times is the most expensive repeat there is (five clamped 212s timeouts is
#: 1060s of a 1200s budget), and it was invisible while errors were skipped.
_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "exec": ("stdout", "stderr", "exit_code", "error"),
    "search_files": ("matches", "count", "truncated", "error"),
    "list_directory": ("path", "entries", "count", "truncated", "error"),
}

#: Fields of an ``exec``-shaped result that carry what the command SAID. A
#: command with nothing in any of them is silent, and a silent command is never
#: refused — see ``_is_silent``.
_SPOKEN_FIELDS: tuple[str, ...] = ("stdout", "stderr", "error")


def output_digest(tool_name: str, result: dict[str, Any]) -> str:
    """Digest the TOOL's own answer, never the engine's annotations.

    The clamp writes ``timeout_note`` — "the run has 242s left" — into every
    clamped ``exec`` result, and the number shrinks with the clock. Digesting
    the whole dict therefore produced a different digest on every call, reset
    the repeat counter every time, and made the repeat guard fire NEVER on the
    exact shape it was built for: nine byte-identical runs of one command, on a
    budget where the clamp is engaged for essentially all of them. Two enforce
    controls cancelling each other, with a test suite that drove them
    separately and saw nothing.
    """
    fields = _OUTPUT_FIELDS.get(tool_name)
    payload = (
        {k: result.get(k) for k in fields if k in result}
        if fields
        else {k: v for k, v in result.items() if k not in _ENGINE_ANNOTATIONS}
    )
    return _digest(payload)


#: Only consulted for a tool with no declared projection above, so that a future
#: guarded tool fails safe (annotations stripped) rather than silently inert.
_ENGINE_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "timeout_note",
        "timeout_seconds",
        "unchanged_since_step",
        "repeat_guard",
        "refused",
        "reason",
    }
)


def _is_silent(tool_name: str, result: dict[str, Any]) -> bool:
    """Did this call say anything at all?

    Output identity is not effect identity, and the proxy is weakest exactly
    where the risk is: the side-effecting commands an agent runs are
    disproportionately SILENT — ``mkdir -p``, ``cp``, ``rm -f``, ``chmod``,
    ``git add``, anything redirected to /dev/null. Their result is
    ``{"stdout": "", "stderr": "", "exit_code": 0}``, byte-identical forever,
    whatever they did, and four identical silent successes establish nothing
    about the fifth — whose inputs the commands in between may have changed.

    So a silent call is still NOTED and never REFUSED. The measured case keeps
    all of its value: ``sam3_debug``'s nine repeats printed an AssertionError.
    """
    if tool_name not in REFUSABLE_TOOLS:
        return True
    return not any(str(result.get(field) or "").strip() for field in _SPOKEN_FIELDS)


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def target_fingerprint(tool_name: str, args: dict[str, Any], workspace: Any) -> str | None:
    """``mtime:size`` of the path this call reads, or None if unknowable.

    None means "do not guard": an unreadable, vanished or unresolvable target
    is re-read so the tool's own error reaches the model, rather than the guard
    answering for a file that is no longer there.
    """
    if tool_name not in SHORT_CIRCUIT_TOOLS:
        return None
    raw = args.get("path", "")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute() and workspace:
            path = Path(workspace) / path
        stat = path.stat()
    except (OSError, ValueError, RuntimeError):
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


@dataclass
class _Read:
    """What the run already knows about one read."""

    step: int
    fingerprint: str
    payload: str  # the serialized result, so it can be resent if context lost
    result: dict[str, Any]


@dataclass
class _Counted:
    """Consecutive occurrences of one call whose output has not changed."""

    runs: int
    digest: str
    silent: bool = False


@dataclass(frozen=True)
class GuardDecision:
    """What the guard decided about a call that is about to be made.

    ``result`` is what dispatch should return INSTEAD of running the tool; None
    means the tool still runs and ``note`` is what the agent is told about it.
    """

    action: str  # "answered" | "noted" | "refused"
    tool_name: str
    note: str
    result: dict[str, Any] | None = None


@dataclass
class RepeatGuard:
    """Per-run repeat state. One instance per run, held on its session."""

    mode: str = "off"
    session: Any = None
    run_id: str = ""
    reads: dict[str, _Read] = field(default_factory=dict)
    counted: dict[str, _Counted] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)
    #: Families of one question asked with varying arguments. Held here rather
    #: than beside it because the two guards share a ladder, a vocabulary and
    #: an evidence table, and an agent told about a repeat twice by two
    #: controls learns nothing the second time.
    variants: VariantTracker = field(default_factory=VariantTracker)
    #: Notes raised mid-iteration, drained by the runner AFTER the tool results
    #: for that iteration are in the conversation. Appending a developer turn
    #: between an assistant's tool_calls message and its tool results would be
    #: rejected by the Anthropic leg, which rewrites developer turns to user
    #: turns (llm_client._normalize_developer_role).
    pending_notes: list[str] = field(default_factory=list)

    # ── decide ──────────────────────────────────────────────────────────

    def before(
        self, tool_name: str, args: dict[str, Any] | None, *, workspace: Any = None
    ) -> GuardDecision | None:
        """The decision for a call about to be made, or None to just run it."""
        if self.mode == "off" or tool_name not in _WATCHED_TOOLS:
            return None
        from robothor.engine.exec_spill import is_spill_readback

        if is_spill_readback(tool_name, args):
            # Paging back the output this engine cut out of a result is the
            # remedy the marker told the agent to use. Answering it with "you
            # already read that" or refusing the fifth one would make the guard
            # the reason the run never sees its own data.
            return None
        try:
            decision = self._decide(tool_name, args or {}, workspace)
        except Exception as exc:  # noqa: BLE001 - a guard never breaks a call
            logger.warning("repeat guard skipped for %s: %s", tool_name, exc)
            return None
        if decision is None:
            return None

        self.counters[f"{tool_name}:{decision.action}"] += 1
        self._record_event(decision)
        if self.mode != "enforce":
            logger.warning(
                "step-efficiency observe: run %s would have %s a repeated %s (%s)",
                self.run_id or "?",
                decision.action,
                tool_name,
                decision.note[:200],
            )
            return None
        logger.warning(
            "repeat guard %s a repeated %s on run %s",
            decision.action,
            tool_name,
            self.run_id or "?",
        )
        if decision.result is None:
            self.pending_notes.append(decision.note)
        else:
            self._remember_answer(tool_name, args or {}, decision.result)
        return decision

    def _decide(self, tool_name: str, args: dict[str, Any], workspace: Any) -> GuardDecision | None:
        """Exact repeats first, then the variants of one question.

        Order is load-bearing. The exact rules can ANSWER a call from what the
        run already holds, which is strictly better than noting it, and a
        variant verdict would otherwise shadow that saving — the short circuit
        is the only rung here that gives the model something back.
        """
        if tool_name in SHORT_CIRCUIT_TOOLS:
            decision = self._decide_read(tool_name, args, workspace)
        elif tool_name in OUTPUT_COUNTED_TOOLS:
            decision = self._decide_counted(tool_name, args)
        else:
            decision = None
        if decision is not None:
            # The agent has just been told. The variant ladder must not say it
            # again — not on this call, and not on the next changed argument,
            # or the exact guard's "any change to the arguments runs
            # immediately" stops being true and a refusal becomes the trap it
            # was designed not to be. Two controls saying the same sentence
            # twice teaches nothing the second time.
            with contextlib.suppress(Exception):
                self.variants.spend(tool_name, args)
            return decision
        return self._decide_variant(tool_name, args)

    def _decide_variant(self, tool_name: str, args: dict[str, Any]) -> GuardDecision | None:
        """The same question asked again with different arguments.

        robothor/engine/repeat_variants.py. Its ladder and vocabulary are this
        one's, so the decision converts straight across and lands in the same
        `agent_guardrail_events` rows.
        """
        verdict = self.variants.verdict(tool_name, args)
        if verdict is None:
            return None
        return GuardDecision(verdict.action, tool_name, verdict.note, verdict.result)

    def _decide_read(
        self, tool_name: str, args: dict[str, Any], workspace: Any
    ) -> GuardDecision | None:
        previous = self.reads.get(canonical_key(tool_name, args))
        if previous is None:
            return None
        fingerprint = target_fingerprint(tool_name, args, workspace)
        if fingerprint is None or fingerprint != previous.fingerprint:
            return None  # changed, gone, or unknowable: read it again

        note = (
            f"identical to your read at step {previous.step}; the content is "
            "unchanged on disk since then"
        )
        # `repeat_guard` is the marker the runner keys on to record this as
        # neither a failure nor progress — the tool did not run.
        result: dict[str, Any] = {
            "unchanged_since_step": previous.step,
            "note": note,
            "repeat_guard": "answered",
        }
        if not self._still_in_context(previous.payload):
            # Compaction took the earlier result out of the conversation, so a
            # pointer to it would point at nothing. Send the content again.
            note = f"{note}, and is repeated here because it is no longer in your context"
            result = {
                **previous.result,
                "unchanged_since_step": previous.step,
                "note": note,
                "repeat_guard": "answered",
            }
        # The decision's note is what `_record_event` writes, so it has to be
        # the note the agent actually got. It was the SHORT text either way,
        # which made a resend and a real saving the same `warned` row — and a
        # sweep counting those rows read 23 full-file resends as 23 answers.
        return GuardDecision("answered", tool_name, note, result)

    def _decide_counted(self, tool_name: str, args: dict[str, Any]) -> GuardDecision | None:
        previous = self.counted.get(canonical_key(tool_name, args))
        if previous is None:
            return None
        occurrence = previous.runs + 1

        if occurrence >= REFUSE_AT and tool_name in REFUSABLE_TOOLS and not previous.silent:
            note = (
                f"This exact command has already run {previous.runs} times with "
                "byte-identical output. Running it again unchanged will not "
                "change the result — change something first: the command, the "
                "code it exercises, or the question you are asking of it."
            )
            # NO `error` key. `runner.py` reads `result.get("error")` straight
            # into `record_tool_outcome`, whose per-tool circuit breaker appends
            # "Tool 'exec' has failed 3 times this run. Do NOT call it again." at
            # three — so a control built to redirect ONE command would have told
            # a Code-task agent to abandon its only way to run code. The same
            # value drives escalation's STOP-RETRYING hints and suppresses the
            # checkpoint's success. A refusal is a redirection, not a fault.
            return GuardDecision(
                "refused",
                tool_name,
                note,
                {
                    "refused": True,
                    "reason": note,
                    "identical_runs": previous.runs,
                    "repeat_guard": "refused",
                },
            )

        if occurrence == NOTE_AT:
            # Present perfect, because `drain_repeat_notes` puts this in the
            # conversation AFTER the third result: by the time the model reads
            # it, "about to run" is already false.
            note = (
                f"[SYSTEM] This exact {tool_name} call has now run {occurrence} times "
                "with identical output. Running it again without changing something "
                "will not change the result. Change the command, change the code it "
                "exercises, or move on to writing what the task asked for."
            )
            return GuardDecision("noted", tool_name, note, None)
        return None

    def _still_in_context(self, payload: str) -> bool:
        """Is the earlier tool result still somewhere the model can read it?"""
        for message in getattr(self.session, "messages", None) or []:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if isinstance(content, str) and payload in content:
                return True
        return False

    def _remember_answer(
        self, tool_name: str, args: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """A resend is a read. Write down what the guard put in the conversation.

        `after` never runs for a call the guard answered — dispatch returns the
        decision and the handler is skipped — so whatever `_remember` last
        stored is what `_still_in_context` keeps comparing against. When the
        answer was a RESEND, that stored payload is a string no message holds
        any more: the conversation now carries the resent dict, which has three
        extra keys and therefore is not a superstring of it. The check was
        false forever after, and the guard resent the same file on every
        subsequent repeat. Measured: `sam3_image.py`, 37 KB, five times in one
        run; 14 of that run's 23 read decisions were this.

        Only the resend needs recording. A short answer added nothing to the
        conversation, so the payload it pointed at is still the right one to
        look for.
        """
        if tool_name not in SHORT_CIRCUIT_TOOLS or "content" not in result:
            return
        previous = self.reads.get(canonical_key(tool_name, args))
        if previous is None:
            return
        try:
            payload = json.dumps(result, default=str)
        except (TypeError, ValueError):  # pragma: no cover - default=str covers these
            return
        if len(payload) > MAX_TRACKED_CHARS:  # pragma: no cover - `after` capped it already
            return
        # Only the payload. `result` stays the handler's own dict, so a resend
        # after a LATER compaction rebuilds byte-identically and this payload
        # goes on being the right string to look for. And the step stays the
        # step the file was actually READ at: the note says "identical to your
        # read at step N", and pointing that at a step where nothing was read
        # would be a lie the model cannot check.
        previous.payload = payload

    # ── remember ────────────────────────────────────────────────────────

    def after(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        result: dict[str, Any] | None,
        *,
        workspace: Any = None,
    ) -> None:
        """Record what a call returned, so the next similar one can be judged."""
        if self.mode == "off" or tool_name not in _WATCHED_TOOLS:
            return
        if not isinstance(result, dict):
            return
        if not ("error" in result and tool_name in SHORT_CIRCUIT_TOOLS):
            # A failed READ is not remembered anywhere — see the branch below,
            # whose rule this shares rather than reinvents.
            with contextlib.suppress(Exception):  # bookkeeping never breaks a run
                self.variants.observe(tool_name, args or {}, result)
        if tool_name not in GUARDED_TOOLS:
            return
        if "error" in result and tool_name in SHORT_CIRCUIT_TOOLS:
            # A failed READ taught the run nothing about the file, and the short
            # circuit has no content it could ever answer with. Errors on the
            # output-counted tools DO count: five identical clamped timeouts is
            # ~1060s of a 1200s budget, the most expensive repeat a run can
            # make, and it was invisible here. Only a byte-identical error text
            # counts, so a retry after a transient failure still reads as
            # progress.
            return
        try:
            self._remember(tool_name, args or {}, result, workspace)
        except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks a run
            logger.warning("repeat guard could not record %s: %s", tool_name, exc)

    def _remember(
        self, tool_name: str, args: dict[str, Any], result: dict[str, Any], workspace: Any
    ) -> None:
        key = canonical_key(tool_name, args)
        if tool_name in SHORT_CIRCUIT_TOOLS:
            fingerprint = target_fingerprint(tool_name, args, workspace)
            if fingerprint is None:
                return
            payload = json.dumps(result, default=str)
            if len(payload) > MAX_TRACKED_CHARS:
                self.reads.pop(key, None)
                return
            existing = self.reads.get(key)
            if existing is not None and existing.fingerprint == fingerprint:
                # Under `observe` the tool keeps running, so without this the
                # record walked forward and the shadow line said "unchanged
                # since step N-1" where `enforce` would have said step 1.
                # Observe evidence that understates enforce is the wrong
                # direction for a promotion gate.
                return
            self.reads[key] = _Read(
                step=self._step(), fingerprint=fingerprint, payload=payload, result=result
            )
            return

        digest = output_digest(tool_name, result)
        silent = _is_silent(tool_name, result)
        previous = self.counted.get(key)
        if previous is None or previous.digest != digest:
            self.counted[key] = _Counted(runs=1, digest=digest, silent=silent)
        else:
            previous.runs += 1
            previous.silent = silent

    def _step(self) -> int:
        return int(getattr(self.session, "_step_counter", 0) or 0) + 1

    def _record_event(self, decision: GuardDecision) -> None:
        """Land the decision in ``agent_guardrail_events`` — the table the
        flag's evidence source reads. Reused, not invented, and best-effort:
        the sweep losing a row must never cost a run a tool call."""
        if not self.run_id:
            return
        try:
            from robothor.engine import tracking

            tracking.log_guardrail_event(
                self.run_id,
                "repeat_guard",
                "observed" if self.mode != "enforce" else _ACTIONS[decision.action],
                tool_name=decision.tool_name,
                reason=decision.note[:500],
                mode=self.mode,
                # Without this every row landed at step 0 and the sweep could
                # not tie a decision to the step it happened on.
                step_number=self._step(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("repeat guard event not recorded: %s", exc)


#: Guard action -> the vocabulary ``agent_guardrail_events`` already uses.
_ACTIONS = {"answered": "warned", "noted": "warned", "refused": "blocked"}


def guard_for_run(run_id: str) -> RepeatGuard | None:
    """The guard for a live run, created once and kept on its session.

    None when no session is registered — an untracked run, or a tool call from
    outside a run — because per-run state with no run to belong to is the kind
    of global that leaks between tenants.
    """
    if not run_id:
        return None
    from robothor.engine import session_registry

    session = session_registry.lookup(run_id)
    if session is None:
        return None
    guard = getattr(session, "repeat_guard", None)
    if guard is None:
        from robothor.engine.run_pacing import mode_for_run

        mode = mode_for_run(run_id)
        if mode == "off":
            # None, not an inert guard: dispatch then skips two
            # `asyncio.to_thread` hops on every tool call, and a disabled
            # control should cost nothing at all.
            return None
        guard = RepeatGuard(mode=mode, session=session, run_id=run_id)
        try:
            session.repeat_guard = guard
        except AttributeError:  # pragma: no cover - a session that refuses state
            return None
    return guard


def drain_repeat_notes(session: Any) -> list[str]:
    """Take the notes the guard raised this iteration, leaving none behind.

    The runner appends these AFTER every tool result for the iteration is in
    the conversation, never between them. A developer turn sitting between an
    assistant's ``tool_calls`` message and its ``tool`` results is rewritten to
    a USER turn on the Anthropic leg (``llm_client._normalize_developer_role``)
    and rejected there, so "inject the note before the tool runs" is bought at
    the price of the fallback chain's last resort. Draining at the end of the
    iteration puts the note in front of the model before its next turn, which
    is the timing that actually matters.
    """
    guard = getattr(session, "repeat_guard", None)
    if guard is None or not guard.pending_notes:
        return []
    notes = list(guard.pending_notes)
    guard.pending_notes.clear()
    return notes
