"""A deliverable whose job is to decide has to decide.

MEASURED 2026-09-16 and twice more on 2026-09-17. A task asked the agent to
triage and route nine messages. One carried a routing-test marker in its own
footer. The agent FOUND the marker and still filed the message as Critical #1
with two named owners, appending *"Please verify whether this is a live
incident or a quarterly routing test."* The rubric was binary, a top-of-report
escalation is an escalation whatever the footnote says, and the item scored
zero. The next two runs said it differently — *"escalated regardless, but
please confirm whether…"*, then *"Treated as a real incident … If this is a
test artefact, please confirm with the owning team"* — and scored zero the same
way. The agent had the evidence, declined to reach a verdict, and handed the
decision back to the reader *inside the artefact whose job was to contain
decisions*. An operator reading that page pages an executive.

This module is the LADDER — task gate, re-ask, guardrail row. What a verdict
and a retraction look like on the page lives in ``verdict_shapes``; what an
item says about itself lives in ``provenance_markers``. It is the narrowest
thing that catches the failure, because it is the only control in this cluster
that touches model JUDGEMENT and a false positive here teaches an agent to
hedge less honestly rather than to decide:

* nothing happens unless the TASK asked for classification — triage, routing,
  prioritisation, severity — in so many words (:func:`asks_for_verdicts`);
* inside such a deliverable four shapes fire: an item under two verdict labels,
  an item whose own metadata contradicts its verdict, an item whose block asks
  the reader to decide, and a verdict withdrawn by a condition about what the
  item is;
* it needs an item IDENTIFIER to anchor on, so prose that merely sounds
  uncertain is invisible to it.

An inline caveat, a confidence note, a stated assumption, a note about
follow-up are all left alone on purpose. "One verdict per item" is not "no
doubt allowed": contradicting evidence resolves INTO the verdict with its
reason rather than going unmentioned.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from robothor.engine.provenance_markers import markers_by_item, tool_result_text
from robothor.engine.verdict_sections import blocks
from robothor.engine.verdict_shapes import (
    MAX_SCAN_CHARS,
    hands_the_verdict_back,
    hedges_the_verdict,
    item_ids,
    overrides_a_marker,
    verdicts_in,
)

__all__ = [
    "Inspection",
    "asks_for_verdicts",
    "findings_for_run",
    "hedged_items",
    "hold_for_hedged_verdicts",
    "inspect_report",
    "inspect_run",
    "record_verdict_findings",
    "verdict_note",
]


class Inspection(NamedTuple):
    """What one pass over a deliverable SAW, not only what it objected to.

    The counts exist because zero findings used to look exactly like a run this
    control never qualified for — the silence described in the module docstring
    above, `feedback-probe-dont-trust-silence`. ``read`` names every deliverable
    this run's task declared and this control could open; ``path`` is the one
    the findings are about.
    """

    path: str
    items: int
    marked: int
    findings: list[tuple[str, str]]
    read: tuple[str, ...] = ()


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

    The findings half of :func:`inspect_report`.
    """
    return inspect_report(report_text, results_text).findings


def inspect_report(report_text: str | None, results_text: str | None = None) -> Inspection:
    """Everything one pass over a deliverable saw: items, markers, findings.

    Four shapes, all anchored on an explicit identifier: the item under two
    DIFFERENT verdict labels; its own metadata (as the run's tools returned it)
    saying it is not a real report of a real event while the verdict files it
    as live work; its own block asking the reader to decide; and a verdict the
    block takes back with a condition about what the item is.

    At most one finding per item, in that order, because the re-ask is a list
    the model has to act on and the most specific reason is the most useful
    one. An item with one verdict and a caveat beside it produces nothing: the
    rule is one verdict, not no doubt.
    """
    text = (report_text or "")[:MAX_SCAN_CHARS]
    if not text:
        return Inspection("", 0, 0, [])
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
        # A marker contradicts only when the report CLASSIFIED the item (an
        # item mentioned in prose has no verdict), gave it live work rather
        # than `no-action` (which is what honouring a marker looks like), and
        # did not override it outright. An "override" that hedges in the same
        # breath — "escalated regardless, but please confirm whether…" — is the
        # measured failure itself, so any hedge on the item cancels it.
        elif (
            item in markers
            and labels
            and "no-action" not in labels
            and not (overridden.get(item) and not handback.get(item) and not hedges.get(item))
        ):
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
    return Inspection("", len(per_item), len(set(per_item) & set(markers)), findings)


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


def _candidate_paths(root: str | None, declared: str) -> list[Path]:
    """Every file the declared path could mean, most specific first.

    ``required_deliverables`` returns the path the TASK wrote — usually
    absolute, in the task's own namespace. The first cut joined it onto the
    workspace root after stripping the leading slash, so
    `/tmp_workspace/results/results.md` became `<root>/tmp_workspace/…`, a path
    that exists nowhere (hostile review, Critical 2): the control only worked
    when the engine ran in the namespace the task described. So: the literal
    path, then the same path re-rooted with each leading component dropped.
    """
    path = Path(declared)
    tried: list[Path] = []
    if path.is_absolute():
        tried.append(path)
        if root:
            parts = path.parts[1:]
            tried += [Path(root).joinpath(*parts[index:]) for index in range(len(parts))]
    else:
        if root:
            tried.append(Path(root) / path)
        tried.append(path)
    return tried


def findings_for_run(
    session: object, workspace: object = None
) -> tuple[str, list[tuple[str, str]]]:
    """``(deliverable path, findings)`` for this run, or ``("", [])``.

    The two fields of :func:`inspect_run` that decide whether anything happens;
    a run with nothing to say names no file.
    """
    inspected = inspect_run(session, workspace)
    return (inspected.path, inspected.findings) if inspected.findings else ("", [])


def inspect_run(session: object, workspace: object = None) -> Inspection:
    """What this run's deliverable said, counts and all.

    The task gate first, always: a run whose task never asked for a decision
    per item is not this module's business and its artefact is never read.

    Every failure below is LOGGED. The first cut wrapped the whole body in a
    bare ``contextlib.suppress(Exception)``, so an unresolvable workspace, an
    unreadable file and a crash inside a detector all returned the same
    ``("", [])`` as a clean deliverable with no line anywhere — a guardrail
    that cannot reach its own input reporting an honest zero forever
    (`feedback-probe-dont-trust-silence`). "Not read" is not "clean".
    """
    logger = logging.getLogger(__name__)
    run_id = str(getattr(getattr(session, "run", None), "id", "") or "?")
    try:
        from robothor.engine.deliverable_contract import task_text_for_run
        from robothor.engine.deliverable_extract import required_deliverables
        from robothor.engine.deliverable_verdict import resolve_workspace

        task_text = task_text_for_run(getattr(session, "run", None), session)
        declared_paths = required_deliverables(task_text) if asks_for_verdicts(task_text) else []
    except Exception:
        logger.warning(
            "verdict commitment: run %s could not read its own task contract", run_id, exc_info=True
        )
        return Inspection("", 0, 0, [])
    if not declared_paths:
        return Inspection("", 0, 0, [])

    root = resolve_workspace(session, str(workspace) if workspace else None)
    results = tool_result_text(session)
    unresolved: list[str] = []
    read = Inspection("", 0, 0, [])
    for declared in declared_paths:
        try:
            found = next((c for c in _candidate_paths(root, declared) if c.is_file()), None)
            if found is None:
                unresolved.append(declared)
                continue
            seen = inspect_report(found.read_text(encoding="utf-8", errors="replace"), results)
        except OSError as exc:
            logger.warning(
                "verdict commitment: run %s could not read the deliverable it was asked for "
                "(%s): %s",
                run_id,
                declared,
                exc.__class__.__name__,
            )
            continue
        except Exception:
            logger.warning(
                "verdict commitment: run %s failed while reading %s — this run is UNCHECKED, "
                "not clean",
                run_id,
                declared,
                exc_info=True,
            )
            continue
        read = Inspection(
            declared,
            read.items + seen.items,
            read.marked + seen.marked,
            seen.findings,
            (*read.read, declared),
        )
        if seen.findings:
            return read
    if unresolved:
        logger.warning(
            "verdict commitment: run %s was asked for %s and no such file exists under %s — "
            "the deliverable was not read, which is not the same as clean",
            run_id,
            ", ".join(unresolved[:3]),
            root or "no resolved workspace",
        )
    return read


def hold_for_hedged_verdicts(session: object, workspace: object = None) -> bool:
    """The agent stopped; does its classification still refuse to classify?

    True means "do not end this iteration". Ladder-gated on
    ``ROBOTHOR_VERDICT_COMMITMENT_MODE``, with the budget
    ``reask_for_wrong_deliverable_shape`` established: at ``enforce`` one
    re-ask, at ``observe`` a WARNING and the run ends, at ``off`` nothing is
    computed. Never raises — a judgement aid that can break a run has a worse
    failure mode than the one it corrects.
    """
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

    Without this the control is INERT in the only place an operator looks:
    ``flags/evidence.py`` points ``ROBOTHOR_VERDICT_COMMITMENT_MODE`` at
    ``agent_guardrail_events``, and a control that writes no row reports an
    honest zero forever however often it fires — the shape this repo has
    recorded twice. This flag's promotion story is "watch the evidence first",
    so the evidence has to exist before the flag ships, and a write that fails
    is logged rather than swallowed for the same reason.

    A FRESH read, like ``record_deliverable_verdicts``: the re-ask exists so
    the agent can fix the file, and it may have done exactly that.

    One INFO line per run that READ a deliverable, findings or none, naming
    every file the counts are over: the run that went silent had read its
    deliverable and found its marker, and nothing said so.
    """
    from robothor.engine.feature_flags import verdict_commitment_mode

    logger = logging.getLogger(__name__)
    mode = verdict_commitment_mode()
    if mode == "off":
        return
    inspected = inspect_run(session, workspace)
    path, findings = inspected.path, inspected.findings
    if inspected.read:
        named = ", ".join(inspected.read[:3])
        extra = len(inspected.read) - 3
        logger.info(
            "verdict commitment %s: run %s inspected %d item(s) in %s — %d carried a "
            "provenance marker, %d finding(s)",
            mode,
            getattr(run, "id", "?"),
            inspected.items,
            f"{named} (+{extra} more)" if extra > 0 else named,
            inspected.marked,
            len(findings),
        )
    if not findings:
        return
    detail = "; ".join(f"{item} {why}" for item, why in findings[:3])
    summary = (
        f"{len(findings)} item(s) in {path or 'the deliverable'} carry no single verdict: {detail}"
    )
    logger.warning(
        "verdict commitment %s: run %s — %s", mode, getattr(run, "id", "?"), summary[:500]
    )
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run.id,  # type: ignore[attr-defined]
            guardrail_name="verdict_commitment",
            action="blocked" if mode == "enforce" else "observed",
            reason=summary[:500],
            mode=mode,
        )
    except Exception:
        logger.warning(
            "verdict commitment: run %s found %d item(s) but its guardrail row was not written",
            getattr(run, "id", "?"),
            len(findings),
            exc_info=True,
        )
