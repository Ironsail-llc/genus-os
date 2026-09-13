"""Thin announce output, and the note the run wrote instead of saying it.

An announce-mode agent sometimes does all of its work, saves the result as a CRM
note (``create_note``, a 1,000+ character body), and then ends the run with a
meta-confirmation: ``"Briefing delivered."`` — 19 characters. Delivery announced
that stub, so the operator's phone showed a header with nothing under it while
the briefing sat in the CRM.

The platform has detected this for a while: ``run_finalizer._assess_outcome``
marks the run ``partial`` and writes *"Thin announce output (N chars) — likely
meta-confirmation instead of full content"*. Detection without recovery is what
this module ends — it is the one place that answers two questions:

1. **Is this output thin?** :func:`is_thin_announce_output`, against
   :data:`ANNOUNCE_MIN_OUTPUT_CHARS`. ``run_finalizer`` flags on the same
   predicate, deliberately: a second threshold in the delivery path would drift
   away from the detector and the two would disagree about the same run.
2. **Did this run write something worth sending instead?**
   :func:`note_body_fallback`.

What may be delivered in the stub's place is narrow on purpose. It is the
``body`` argument of a ``create_note`` call **the agent itself authored**, in
**this** run, and only when that body is substantial by the same threshold and
longer than the text it replaces. Never a tool's *output*: a search result or a
fetched page is something the agent read, not something it decided to say, and
announcing one would put text nobody wrote in front of the operator.

Why the run's own steps rather than a CRM query: the steps carry the run id, so
"the note this run wrote" is answerable without trusting a title match or a
timestamp window — a note from another run can never be borrowed. It is also
the same in-memory trace ``delivery._reframe_beat_output`` already reads, so the
fallback costs no database round trip on the delivery path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.models import AgentRun

__all__ = [
    "ANNOUNCE_MIN_OUTPUT_CHARS",
    "THIN_FALLBACK_NOTE",
    "is_thin_announce_output",
    "note_body_fallback",
    "record_note_fallback",
]

#: Announce-mode runs that end with fewer characters than this are flagged
#: ``partial`` — almost always a meta-confirmation ("briefing delivered")
#: rather than the real content the agent was supposed to broadcast.
#: ``run_finalizer`` imports this name; it is not redefined there.
ANNOUNCE_MIN_OUTPUT_CHARS = 200

#: What ``run.outcome_notes`` records when the fallback fired. A substituted
#: body must be visible to whoever reads the run back — the delivered text then
#: differs from ``agent_runs.output_text``, and an unexplained difference
#: between those two is indistinguishable from a bug.
THIN_FALLBACK_NOTE = "delivered note body instead of thin output"

#: The one tool whose argument may stand in for the agent's final word.
_NOTE_TOOL = "create_note"


def is_thin_announce_output(text: str | None) -> bool:
    """True when ``text`` is too short to be the content an announce promised."""
    return len((text or "").strip()) < ANNOUNCE_MIN_OUTPUT_CHARS


def note_body_fallback(run: AgentRun, text: str | None) -> str | None:
    """The note body this run wrote that should be announced instead of ``text``.

    Returns ``None`` — meaning "deliver what the run said" — unless every one of
    these holds:

    * ``text`` is thin by :func:`is_thin_announce_output`;
    * a ``create_note`` step belonging to THIS run recorded a string ``body``;
    * that body is itself substantial by the same threshold (a note saying
      "Briefing saved." is a second stub, not a rescue); and
    * it is longer than ``text``, so the fallback can only ever add content.

    Steps whose ``run_id`` names a different run are skipped even when they are
    sitting in ``run.steps``: the scoping is what makes "the note this run
    wrote" a fact rather than a guess. With several qualifying notes the longest
    wins — a briefing run that also filed a short side note should broadcast the
    briefing.

    Never raises. A delivery that fails because the rescue path threw would be
    strictly worse than the stub this exists to replace.
    """
    if not is_thin_announce_output(text):
        return None
    stripped = (text or "").strip()
    run_id = str(getattr(run, "id", "") or "")
    best: str | None = None
    for step in getattr(run, "steps", None) or []:
        body = _authored_note_body(step, run_id)
        if body is None or len(body) <= len(stripped):
            continue
        if best is None or len(body) > len(best):
            best = body
    return best


def _authored_note_body(step: Any, run_id: str) -> str | None:
    """The substantial ``create_note`` body on ``step``, if it has one."""
    if (getattr(step, "tool_name", None) or "") != _NOTE_TOOL:
        return None
    step_run_id = str(getattr(step, "run_id", "") or "")
    if run_id and step_run_id and step_run_id != run_id:
        return None
    payload = getattr(step, "tool_input", None)
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    if not isinstance(body, str):
        return None
    body = body.strip()
    return None if is_thin_announce_output(body) else body


def record_note_fallback(run: AgentRun) -> None:
    """Note on ``run`` that the delivered body was the note, not the output.

    Appended rather than assigned: the ``Thin announce output (N chars)`` note
    ``run_finalizer`` wrote is the reason this fired, and keeping both means the
    row says what happened and what was done about it.
    """
    existing = getattr(run, "outcome_notes", None)
    run.outcome_notes = f"{existing}; {THIN_FALLBACK_NOTE}" if existing else THIN_FALLBACK_NOTE
