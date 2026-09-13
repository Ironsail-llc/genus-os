"""Thin announce output, and the note the run wrote instead of saying it.

An announce-mode agent sometimes does all of its work, saves the result as a CRM
note (``create_note``, a 1,000+ character body), and then ends the run with a
meta-confirmation: ``"Briefing delivered."`` — 19 characters. Delivery announced
that stub, so the operator's phone showed a header with nothing under it while
the briefing sat in the CRM.

The platform has detected this for a while: ``run_finalizer._assess_outcome``
marks the run ``partial`` and writes *"Thin announce output (N chars) — likely
meta-confirmation instead of full content"*. Detection without recovery is what
this module ends — it is the one place that answers three questions:

1. **Is this output thin?** :func:`is_thin_announce_output`, against
   :data:`ANNOUNCE_MIN_OUTPUT_CHARS`. ``run_finalizer`` flags on the same
   predicate, deliberately: a second threshold in the delivery path would drift
   away from the detector and the two would disagree about the same run.
2. **Did this run write something worth sending instead?**
   :func:`note_substitution`.
3. **What does the row then say happened?** :func:`substitution_note` — and it
   says only what has been checked. See below.

What may be delivered in the stub's place is narrow on purpose. It is the
``body`` argument of a ``create_note`` call **the agent itself authored**, in
**this** run, and only when that body is substantial by the same threshold. Never
a tool's *output*: a search result or a fetched page is something the agent read,
not something it decided to say. And never another tool's ``body`` argument —
``gws_gmail_send`` has one too, and an outbound email addressed to a third party
is not the operator's briefing.

Why the run's own steps rather than a CRM query: the steps carry the run id, so
"the note this run wrote" is answerable without trusting a title match or a
timestamp window — a note from another run can never be borrowed. It is also
the same in-memory trace ``delivery._reframe_beat_output`` already reads, so the
fallback costs no database round trip on the delivery path.

**A note whose save FAILED still supplies its body.** The content is
agent-authored and addressed to the operator, and losing it is the exact defect
this module exists to fix. What the row must not do is claim a note was filed
when the write was refused, or claim a delivery the receipt did not support —
so :func:`substitution_note` is written from the ``create_note`` step's own error
state and from the ``delivery_status`` the channel's receipt produced, after the
send, and never from the fact that a substitution happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.models import AgentRun

__all__ = [
    "ANNOUNCE_MIN_OUTPUT_CHARS",
    "SUBSTITUTION_PREFIX",
    "NoteSubstitution",
    "is_thin_announce_output",
    "note_substitution",
    "record_substitution",
    "substitution_note",
]

#: Announce-mode runs that end with fewer characters than this are flagged
#: ``partial`` — almost always a meta-confirmation ("briefing delivered")
#: rather than the real content the agent was supposed to broadcast.
#: ``run_finalizer`` imports this name; it is not redefined there, and
#: ``test_delivery_thin_announce`` fails if any other engine module defines it.
ANNOUNCE_MIN_OUTPUT_CHARS = 200

#: Every outcome note this module writes starts with these words, which is what
#: makes a second ``deliver()`` on the same run replace its note rather than
#: append a second one.
SUBSTITUTION_PREFIX = "substituted note body"

#: The separator ``outcome_notes`` is joined with. One column, one separator:
#: ``run_finalizer._note_verification`` and ``loop_guards`` use this too.
NOTE_SEPARATOR = "; "

#: The one tool whose argument may stand in for the agent's final word.
_NOTE_TOOL = "create_note"


@dataclass(frozen=True)
class NoteSubstitution:
    """The note body to announce, and whether the note itself was saved."""

    body: str
    saved: bool


def is_thin_announce_output(text: str | None) -> bool:
    """True when ``text`` is too short to be the content an announce promised."""
    return len((text or "").strip()) < ANNOUNCE_MIN_OUTPUT_CHARS


def note_substitution(run: AgentRun, text: str | None) -> NoteSubstitution | None:
    """The note this run wrote that should be announced instead of ``text``.

    Returns ``None`` — meaning "deliver what the run said" — unless all of:

    * ``text`` is thin by :func:`is_thin_announce_output`;
    * a ``create_note`` step belonging to THIS run recorded a string ``body``;
      a step whose ``run_id`` is empty or names another run is refused, which is
      what makes "the note this run wrote" a fact rather than a guess;
    * that body is itself substantial by the same threshold — a note saying
      "Briefing saved." is a second stub, not a rescue.

    A substantial body is by construction longer than a thin text (one
    threshold decides both), so no separate length comparison is made: a guard
    that cannot fail is one this codebase has shipped too often already.

    **The most recently authored note wins** — the highest ``step_number``, not
    the longest body. An agent that files a long research note and then the
    short final briefing must broadcast the briefing. Step numbers are unique
    within a run (``session.AgentSession`` increments one counter), so a tie is
    not reachable in production; should one ever appear, the LAST such step in
    ``run.steps`` wins. Position alone is never trusted — a step list that
    arrived out of order still resolves to the highest number.

    Never raises. A delivery that fails because the rescue path threw would be
    strictly worse than the stub this exists to replace.
    """
    if not is_thin_announce_output(text):
        return None
    run_id = str(getattr(run, "id", "") or "")
    if not run_id:
        return None
    best: tuple[int, NoteSubstitution] | None = None
    for step in getattr(run, "steps", None) or []:
        candidate = _authored_note(step, run_id)
        if candidate is None:
            continue
        rank = int(getattr(step, "step_number", 0) or 0)
        if best is None or rank >= best[0]:
            best = (rank, candidate)
    return best[1] if best else None


def _authored_note(step: Any, run_id: str) -> NoteSubstitution | None:
    """The substantial ``create_note`` body on ``step``, if it has one."""
    if (getattr(step, "tool_name", None) or "") != _NOTE_TOOL:
        return None
    if str(getattr(step, "run_id", "") or "") != run_id:
        return None
    payload = getattr(step, "tool_input", None)
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    if not isinstance(body, str):
        return None
    body = body.strip()
    if is_thin_announce_output(body):
        return None
    return NoteSubstitution(body=body, saved=_note_saved(step))


def _note_saved(step: Any) -> bool:
    """Whether the ``create_note`` call itself succeeded.

    Two shapes, because the tool reports failure in both: the step's own
    ``error_message`` (raised or timed out) and the handler's
    ``{"error": "Failed to create note"}`` return.
    """
    if getattr(step, "error_message", None):
        return False
    output = getattr(step, "tool_output", None)
    return not (isinstance(output, dict) and output.get("error"))


def substitution_note(substitution: NoteSubstitution, delivery_status: str | None) -> str:
    """The exact ``outcome_notes`` entry for a substitution that has been sent.

    Called with the status the receipt produced, so the row never claims reach
    the channel did not acknowledge: ``delivered`` is the only status that means
    a person saw it (``docs/SYSTEM_ARCHITECTURE.md``, delivery status
    vocabulary), and everything else — ``partial:1/3``, any ``failed:`` — reads
    as a failed send here.
    """
    if delivery_status != "delivered":
        return f"{SUBSTITUTION_PREFIX} — send failed: {delivery_status or 'unrecorded'}"
    filed = "saved" if substitution.saved else "note save failed"
    return f"{SUBSTITUTION_PREFIX} ({filed}) — delivered"


def record_substitution(
    run: AgentRun, substitution: NoteSubstitution, delivery_status: str | None
) -> None:
    """Record the substitution on ``run.outcome_notes``, exactly once.

    Appended rather than assigned: the ``Thin announce output (N chars)`` note
    ``run_finalizer`` wrote is the reason this fired, and keeping both means the
    row says what happened and what was done about it. A previous substitution
    note is REPLACED rather than joined — a second ``deliver()`` of the same run
    (a retry that lands after a failed send) must leave one current statement,
    not two contradicting ones.
    """
    note = substitution_note(substitution, delivery_status)
    kept = [
        part
        for part in (getattr(run, "outcome_notes", None) or "").split(NOTE_SEPARATOR)
        if part.strip() and not part.strip().startswith(SUBSTITUTION_PREFIX)
    ]
    kept.append(note)
    run.outcome_notes = NOTE_SEPARATOR.join(kept)
