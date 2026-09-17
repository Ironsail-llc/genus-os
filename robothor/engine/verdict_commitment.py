"""A deliverable whose job is to decide has to decide.

MEASURED 2026-09-16, and twice more on 2026-09-17. A task asked the agent to
triage and route nine messages. One of them carried a routing-test marker in
its own footer. The agent FOUND the marker — and still filed the message as
Critical #1 with two named owners, appending *"Please verify whether this is a
live incident or a quarterly routing test before committing full engineering
resources."* The rubric was binary, a top-of-report escalation is an escalation
whatever the footnote says, and the item scored zero. The next two runs said it
differently — *"escalated regardless, but please confirm whether…"*, then
*"Treated as a real incident … If this is a test artefact, please confirm with
the owning team"* — and scored zero the same way.

That is not a perception failure. The agent had the evidence, declined to reach
a verdict, and handed the decision back to the reader *inside the artefact whose
job was to contain decisions*. An operator reading that page pages an executive.

This module is the LADDER — the task gate, the re-ask, the guardrail row. What
a verdict and a retraction look like on the page lives in ``verdict_shapes``;
what an item says about itself lives in ``provenance_markers``. It is
deliberately the narrowest thing that catches the failure, because it is the
only control in this cluster that touches model JUDGEMENT and a false positive
here teaches an agent to hedge less honestly rather than to decide:

* it does nothing at all unless the TASK asked for classification — triage,
  routing, prioritisation, severity — in so many words (:func:`asks_for_verdicts`);
* inside such a deliverable it fires on four shapes: an enumerated item under
  two different verdict labels, an item whose own metadata contradicts the
  verdict it was given, an item whose block asks the reader to decide, and a
  verdict withdrawn by a condition about what the item is;
* it needs an item IDENTIFIER to anchor on, so prose that merely sounds
  uncertain is invisible to it.

Everything else — an inline caveat, a confidence note, a stated assumption, a
note about follow-up — is left alone on purpose. "One verdict per item" is not
"no doubt allowed": the rule is that contradicting evidence resolves INTO the
verdict with its reason, not that it goes unmentioned.
"""

from __future__ import annotations

import re

from robothor.engine.provenance_markers import markers_by_item, tool_result_text
from robothor.engine.verdict_shapes import (
    MAX_SCAN_CHARS,
    blocks,
    hands_the_verdict_back,
    hedges_the_verdict,
    item_ids,
    overrides_a_marker,
    verdicts_in,
)

__all__ = [
    "asks_for_verdicts",
    "findings_for_run",
    "hedged_items",
    "hold_for_hedged_verdicts",
    "record_verdict_findings",
    "verdict_note",
]

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

#: And it must ask for it PER ITEM, in so many words. The word is what
#: separates "triage this incident" from "triage these incidents", and only the
#: second is a contract this module has anything to say about.
#:
#: ``per \w+`` used to be one of these alternatives and it was far too loose:
#: it opened the gate on "escalate blockers … **as per** the runbook" and on
#: "extract the tables, one **per page**, and rank them" (hostile review I7).
#: Neither asks for a decision about anything. What is left is explicit
#: distribution over items, plus the two phrasings that state the
#: one-category-per-item contract outright.
_PER_ITEM = re.compile(
    r"\b(?:each|every|for\s+each|all\s+(?:of\s+)?the)\b"
    r"|\bexactly\s+one\b"
    r"|\bone\s+of\s+the\s+following\b",
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


def hedged_items(report_text: str | None, results_text: str | None = None) -> list[tuple[str, str]]:
    """``(item id, why)`` for every item this deliverable failed to decide.

    Four shapes, all anchored on an explicit identifier:

    * the item appears under two DIFFERENT verdict labels;
    * its own metadata, as the run's tools returned it, says it is not a real
      report of a real event, and the verdict files it as live work anyway;
    * the item's own block asks the reader to decide;
    * the block states a verdict and then takes it back with a condition about
      what the item is.

    At most one finding per item, in that order, because the re-ask is a list
    the model has to act on and the most specific reason is the most useful
    one. An item with one verdict and a caveat beside it produces nothing,
    which is the whole point: the rule is one verdict, not no doubt.
    """
    text = (report_text or "")[:MAX_SCAN_CHARS]
    if not text:
        return []
    markers = markers_by_item(results_text)
    per_item: dict[str, set[str]] = {}
    handback: dict[str, bool] = {}
    hedges: dict[str, str] = {}
    overridden: dict[str, bool] = {}
    for block in blocks(text):
        found = verdicts_in(block)
        asks_reader = hands_the_verdict_back(block)
        hedge = hedges_the_verdict(block)
        override = overrides_a_marker(block)
        for item in item_ids(block):
            per_item.setdefault(item, set()).update(found)
            if asks_reader:
                handback[item] = True
            if hedge:
                hedges.setdefault(item, hedge)
            if override:
                overridden[item] = True

    findings: list[tuple[str, str]] = []
    for item in sorted(per_item):
        labels = per_item[item]
        if len(labels) > 1:
            findings.append(
                (item, f"appears under {len(labels)} verdicts ({', '.join(sorted(labels))})")
            )
        elif _marker_is_contradicted(item, labels, markers, handback, hedges, overridden):
            findings.append(
                (
                    item,
                    f'its own metadata says "{markers[item]}", and the verdict '
                    "neither honours it nor says what overrides it",
                )
            )
        elif handback.get(item):
            findings.append((item, "asks the reader to decide rather than deciding"))
        elif hedges.get(item):
            findings.append((item, f'states a verdict and takes it back: "{hedges[item]}"'))
    return findings


def _marker_is_contradicted(
    item: str,
    labels: set[str],
    markers: dict[str, str],
    handback: dict[str, bool],
    hedges: dict[str, str],
    overridden: dict[str, bool],
) -> bool:
    """True when the item's own marker disagrees with what the report did.

    Three conditions, all of them required. The report has to have CLASSIFIED
    the item — an item mentioned in prose is not a verdict. The verdict has to
    be live work rather than ``no-action``, which is what honouring the marker
    looks like. And the block must not have overridden the marker outright —
    though an "override" that hedges in the same breath ("escalated
    regardless, but please confirm whether…") is the measured failure itself,
    not an override, so a hedge anywhere on the item cancels it.
    """
    if item not in markers or not labels or "no-action" in labels:
        return False
    return not (overridden.get(item) and not handback.get(item) and not hedges.get(item))


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
        "report escalation with a caveat underneath it is still an escalation. An item's "
        "own provenance marker — what produced it, and what it says it is — is evidence "
        "about that item: honour it, or name the evidence that overrides it."
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
            findings = hedged_items(
                candidate.read_text(encoding="utf-8", errors="replace"),
                tool_result_text(session),
            )
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
