"""A deliverable whose job is to decide has to decide.

MEASURED 2026-09-16. A task asked the agent to triage and route nine messages.
One of them, ``msg_2209``, carried a planted QA-routing marker in its footer.
The agent FOUND the marker — and still filed the message as Critical #1 with two
named owners, appending *"Please verify whether this is a live incident or a
quarterly QA routing test before committing full engineering resources."* The
rubric was binary, a top-of-report P0 is an escalation whatever the footnote
says, and the item scored zero.

That is not a perception failure. The agent had the evidence, declined to reach
a verdict, and handed the decision back to the reader *inside the artefact whose
job was to contain decisions*. An operator reading that page pages the CEO.

This module is deliberately the narrowest thing that catches it, because it is
the only control in this change that touches model JUDGEMENT and a false
positive here teaches an agent to hedge less honestly rather than to decide:

* it does nothing at all unless the TASK asked for classification — triage,
  routing, prioritisation, severity — in so many words (:func:`asks_for_verdicts`);
* inside such a deliverable it fires on two shapes only: an enumerated item that
  appears under two different verdict labels, and an item whose own block asks
  the reader to decide;
* it needs an item IDENTIFIER to anchor on, so prose that merely sounds
  uncertain is invisible to it.

Everything else — an inline caveat, a confidence note, a stated assumption — is
left alone on purpose. "One verdict per item" is not "no doubt allowed": the
rule is that contradicting evidence resolves INTO the verdict with its reason,
not that it goes unmentioned.
"""

from __future__ import annotations

import re

__all__ = [
    "asks_for_verdicts",
    "findings_for_run",
    "hedged_items",
    "hold_for_hedged_verdicts",
    "record_verdict_findings",
    "verdict_note",
]

#: How much of a task or a deliverable is scanned. Bounded for the same reason
#: `deliverable_extract` bounds its own scan: a 3 MB file must cost a bounded
#: amount of work, not an unbounded one.
MAX_SCAN_CHARS = 64 * 1024

#: The task must ASK for a decision per item. Every verb here takes the items
#: as its object; "summarise", "list" and "report on" are deliberately absent,
#: because a summary that reports two possibilities is doing its job.
_ASKS = re.compile(
    r"\b(?:triage|classify|classif(?:y|ication)|categor(?:ise|ize)|"
    r"priorit(?:ise|ize|isation|ization)|route|routing|"
    r"assign\s+(?:a\s+)?(?:severity|priority|owner)|"
    r"escalat(?:e|ion)|rank)\b",
    re.IGNORECASE,
)

#: And it must ask for it PER ITEM. One word, but it is the word that separates
#: "triage this incident" from "triage these incidents", and only the second is
#: a contract this module has anything to say about.
_PER_ITEM = re.compile(
    r"\b(?:each|every|all\s+(?:of\s+)?the|per\s+\w+|for\s+each)\b", re.IGNORECASE
)

#: What an enumerated item looks like. Three shapes, all of them explicit
#: identifiers rather than anything inferred: `msg_2209`, `#12`, `TASK-4`.
_ITEM_ID = re.compile(r"\b[a-z][a-z0-9]{1,12}_\d{2,}\b|\B#\d{1,5}\b|\b[A-Z]{2,6}-\d{1,6}\b")

#: The verdict vocabulary. A closed list, because an open one would read a
#: paragraph's adjectives as verdicts. Each entry is a label a triage
#: deliverable puts at the head of a section.
_VERDICTS: dict[str, re.Pattern[str]] = {
    "critical": re.compile(r"\b(?:critical|p0|sev\s*0|sev\s*1|highest)\b", re.IGNORECASE),
    "high": re.compile(r"\b(?:high(?:\s+priority)?|p1|urgent)\b", re.IGNORECASE),
    "medium": re.compile(r"\b(?:medium(?:\s+priority)?|moderate|p2)\b", re.IGNORECASE),
    "low": re.compile(r"\b(?:low(?:\s+priority)?|p3|p4|minor)\b", re.IGNORECASE),
    "no-action": re.compile(
        r"\b(?:no\s+action|not\s+escalated?|ignore[ds]?|dismissed?|false\s+positive|"
        r"test(?:\s+message)?|drill|qa\s+(?:test|routing))\b",
        re.IGNORECASE,
    ),
}

#: Where a verdict is ASSIGNED rather than merely mentioned: a heading, a
#: bolded lead, or a named field. Each alternative is anchored and bounded, so
#: the whole thing stays linear on a hostile document.
_LABEL = re.compile(
    r"^#{1,6}[ \t]*([^\n]{0,200})$"
    r"|^[ \t]*(?:[-*+][ \t]+)?\*\*([^*\n]{0,80})\*\*"
    r"|^[ \t]*\|?[ \t]*(?:severity|priority|verdict|status|classification|disposition|action)"
    r"[ \t]*[:=|][ \t]*([^\n|]{0,60})",
    re.IGNORECASE | re.MULTILINE,
)

#: The decision handed back to the reader. Narrow and literal: this is the
#: sentence the measured run appended, and variants of it.
_HANDBACK = re.compile(
    r"\b(?:please|you\s+(?:should|may\s+wish\s+to)|the\s+(?:reader|operator|team)\s+should)\s+"
    r"(?:verify|confirm|decide|determine|check|establish)\s+(?:whether|if)\b"
    r"|\bit\s+is\s+(?:unclear|ambiguous|uncertain)\s+whether\b"
    r"|\b(?:someone|a\s+human)\s+(?:should|must)\s+decide\b",
    re.IGNORECASE,
)


def asks_for_verdicts(task_text: str | None) -> bool:
    """True when the TASK asked for a decision on each of several items.

    Both halves required. A task that says "triage this alert" is not asking
    for a table of verdicts, and a task that says "for each file" is not asking
    for a verdict at all — it is the conjunction that names the contract.
    """
    text = (task_text or "")[:MAX_SCAN_CHARS]
    if not text:
        return False
    return bool(_ASKS.search(text)) and bool(_PER_ITEM.search(text))


def _blocks(text: str) -> list[str]:
    """The document cut into item-sized pieces.

    On markdown headings where there are any, on blank lines where there are
    not. The cut only decides how far a verdict reaches from its item; both
    detectors below re-anchor on the item id itself.
    """
    if re.search(r"^#{1,6}\s", text, re.MULTILINE):
        parts = re.split(r"^(?=#{1,6}\s)", text, flags=re.MULTILINE)
    else:
        parts = re.split(r"\n\s*\n", text)
    return [part for part in parts if part.strip()]


def _verdicts_in(chunk: str) -> set[str]:
    """The verdicts this block ASSIGNS, read only from label positions.

    Scanning the whole block was the first cut and it was wrong in both
    directions, caught by its own tests: "Confidence is moderate" in a sentence
    read as a *medium* verdict, and the measured report's own hedge read as a
    second verdict rather than as a hand-back. A verdict is something a triage
    document puts in a heading, a bolded lead or a ``Severity:`` field —
    prose that happens to contain the word is discussion, not a decision.
    """
    labels = " | ".join(part for match in _LABEL.finditer(chunk) for part in match.groups() if part)
    return {name for name, pattern in _VERDICTS.items() if pattern.search(labels)}


def hedged_items(report_text: str | None) -> list[tuple[str, str]]:
    """``(item id, why)`` for every item this deliverable failed to decide.

    Two shapes, both anchored on an explicit identifier:

    * the item appears under two DIFFERENT verdict labels — filed as Critical
      here and as a QA test there;
    * the item's own block asks the reader to decide.

    An item with one verdict and a caveat beside it produces nothing, which is
    the whole point: the rule is one verdict, not no doubt.
    """
    text = (report_text or "")[:MAX_SCAN_CHARS]
    if not text:
        return []
    per_item: dict[str, set[str]] = {}
    handback: dict[str, bool] = {}
    for block in _blocks(text):
        found = _verdicts_in(block)
        asks_reader = bool(_HANDBACK.search(block))
        for item in set(_ITEM_ID.findall(block)):
            per_item.setdefault(item, set()).update(found)
            if asks_reader:
                handback[item] = True

    findings: list[tuple[str, str]] = []
    for item in sorted(per_item):
        labels = per_item[item]
        if len(labels) > 1:
            findings.append(
                (item, f"appears under {len(labels)} verdicts ({', '.join(sorted(labels))})")
            )
        elif handback.get(item):
            findings.append((item, "asks the reader to decide rather than deciding"))
    return findings


def verdict_note(findings: list[tuple[str, str]], path: str = "") -> str:
    """The one ask, or "" when there is nothing to ask about."""
    if not findings:
        return ""
    where = f" in {path}" if path else ""
    lines = "\n".join(f"- {item}: {why}" for item, why in findings[:5])
    return (
        f"[SYSTEM] This task asked you to decide, and{where} some items carry no single "
        f"decision:\n{lines}\n"
        "Pick one verdict for each and say why, folding the contradicting evidence INTO "
        "that verdict as its reason. A reader who has to choose between two verdicts in "
        "your report has been handed the decision your report was for — and a top-of-"
        "report escalation with a caveat underneath it is still an escalation."
    )


def findings_for_run(
    session: object, workspace: object = None
) -> tuple[str, list[tuple[str, str]]]:
    """``(deliverable path, findings)`` for this run, or ``("", [])``.

    The task gate first, always: a run whose task never asked for a decision
    per item is not this module's business and its artefact is never read.
    """
    import contextlib
    from pathlib import Path

    findings: list[tuple[str, str]] = []
    with contextlib.suppress(Exception):
        from robothor.engine.deliverable_contract import task_text_for_run
        from robothor.engine.deliverable_extract import required_deliverables
        from robothor.engine.deliverable_verdict import resolve_workspace

        task_text = task_text_for_run(getattr(session, "run", None), session)
        if not asks_for_verdicts(task_text):
            return "", []
        root = resolve_workspace(session, str(workspace) if workspace else None)
        for declared in required_deliverables(task_text):
            candidate = Path(root or ".") / declared.lstrip("/")
            if not candidate.is_file():
                candidate = Path(declared)
            if not candidate.is_file():
                continue
            findings = hedged_items(candidate.read_text(encoding="utf-8", errors="replace"))
            if findings:
                return declared, findings
    return "", findings


def hold_for_hedged_verdicts(session: object, workspace: object = None) -> bool:
    """The agent stopped; does its classification still refuse to classify?

    True means "do not end this iteration". Ladder-gated on
    ``ROBOTHOR_VERDICT_COMMITMENT_MODE``, with the budget and the shape
    ``reask_for_wrong_deliverable_shape`` established: at ``enforce`` one
    re-ask with the findings, at ``observe`` a WARNING and the run ends, at
    ``off`` nothing is computed. Never raises — a judgement aid that can break
    a run has a worse failure mode than the one it corrects.
    """
    import logging

    from robothor.engine.feature_flags import verdict_commitment_mode

    logger = logging.getLogger(__name__)
    mode = verdict_commitment_mode()
    if mode == "off":
        return False
    path, findings = findings_for_run(session, workspace)
    if not findings:
        return False
    run_id = str(getattr(getattr(session, "run", None), "id", "") or "?")
    if mode != "enforce":
        logger.warning(
            "verdict commitment observe: run %s would be re-asked about %s",
            run_id,
            "; ".join(f"{item} {why}" for item, why in findings[:3]),
        )
        return False
    if int(getattr(session, "_verdict_reasks", 0) or 0) >= 1:
        return False
    note = verdict_note(findings, path)
    if not note:
        return False
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    session._verdict_reasks = 1  # type: ignore[attr-defined]
    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": note})  # type: ignore[attr-defined]
    logger.warning("verdict commitment enforce: run %s re-asked once for %s", run_id, path or "?")
    return True


def record_verdict_findings(run: object, session: object, workspace: object = None) -> None:
    """The end-of-run row, on ``observe`` as well as ``enforce``.

    Without this the control is INERT in the only place an operator looks.
    ``flags/evidence.py`` points ``ROBOTHOR_VERDICT_COMMITMENT_MODE`` at
    ``agent_guardrail_events`` with ``guardrail_name = 'verdict_commitment'``,
    and a control that writes no row reports an honest zero forever however
    often it actually fires — the shape this repo has now recorded twice, as
    controls armed and aimed at nothing and as a guard whose silence was read
    as an absence of violations. This flag's entire promotion story is "watch
    the evidence first", so the evidence has to exist before the flag ships.

    A FRESH read, like ``record_deliverable_verdicts``: the re-ask exists so
    the agent can fix the file, and between the loop's read and this one it
    may have done exactly that.
    """
    import contextlib
    import logging

    from robothor.engine.feature_flags import verdict_commitment_mode

    mode = verdict_commitment_mode()
    if mode == "off":
        return
    path, findings = findings_for_run(session, workspace)
    if not findings:
        return
    detail = "; ".join(f"{item} {why}" for item, why in findings[:3])
    summary = (
        f"{len(findings)} item(s) in {path or 'the deliverable'} carry no single verdict: {detail}"
    )
    logging.getLogger(__name__).warning(
        "verdict commitment %s: run %s — %s", mode, getattr(run, "id", "?"), summary[:500]
    )
    with contextlib.suppress(Exception):
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run.id,  # type: ignore[attr-defined]
            guardrail_name="verdict_commitment",
            action="blocked" if mode == "enforce" else "observed",
            reason=summary[:500],
            mode=mode,
        )
