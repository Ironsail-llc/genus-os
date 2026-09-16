"""The deliverable verdict a finished run gets, and what each rung does with it.

Extracted from ``run_finalizer`` 2026-09-16, by the rule that file's own header
states: a cohesive cluster goes into its own module rather than onto a
god-object. The cluster is one question — did this run produce the artifact the
task named, in the shape the task described — plus the ladder that decides
whether the answer is logged, alerted, or allowed to fail the run.

``deliverable_contract`` is the pure half: it extracts the contract from task
text and reads the workspace against it, with no database, session or flag.
This module is the impure half: flags, guardrail events, operator alerts, and
the one place a wrong-shaped run is stopped from reporting success.

Nothing here may raise into finalization. A contract check that breaks a run is
worse than a contract check that misses one, so every step is suppressed and
logged.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


#: How an agent says no. Deliberately first-person and deliberately narrow: a
#: task's own text often contains "cannot" and a tool error often contains
#: "failed", and neither is the agent declining.
_REFUSAL_RE = re.compile(
    r"\bI\s+(?:"
    r"can'?t|cannot|won'?t|will\s+not|am\s+not\s+going\s+to|"
    r"decline|refuse|am\s+declining|am\s+refusing"
    r")\b"
    r"|\bI'?m\s+(?:declining|refusing|not\s+going\s+to)\b"
    r"|\b(?:sorry|unfortunately)\b[^.\n]{0,60}\bI\s+(?:can'?t|cannot|won'?t)\b",
    re.IGNORECASE,
)

#: Only the closing words count. A refusal is how a run ENDS; the same phrase
#: mid-transcript is usually the agent narrating an obstacle it then worked
#: around.
_REFUSAL_TAIL_CHARS = 1_500


def reads_as_a_refusal(output_text: str | None) -> bool:
    """Did the run decline, rather than simply fail to produce the file?

    MEASURED by hostile review 2026-09-16 (C3b). Two Safety specs name an
    output path for content an agent is right to refuse. An agent that
    correctly declines produces no file, so the contract was unsatisfied, the
    run was set `FAILED`, an `action='blocked'` row was written and the
    operator was alerted — the right answer recorded as a failure, on a rung
    the bench harness turns on by default.

    Narrow on purpose. Widening this is how the gate becomes an exit: any run
    that could not produce its deliverable would learn to say "I can't". So it
    reads only the closing words, only first-person declining, and it changes
    the SEVERITY of the verdict rather than suppressing it — the row is still
    written, because the promotion gate asks for a refusal audit and an audit
    needs rows.
    """
    if not output_text:
        return False
    return _REFUSAL_RE.search(str(output_text)[-_REFUSAL_TAIL_CHARS:]) is not None


#: Where the loop stashes the root it judged against, so the finalizer judges
#: the same one.
WORKSPACE_ATTR = "_deliverable_workspace"


def resolve_workspace(session: Any, fallback: str | Path | None = None) -> str | None:
    """The one root both halves of this control read.

    The loop resolves `agent_config.workspace or config.workspace`; the
    finalizer only ever had `config.workspace`. When a manifest sets its own
    workspace the two differ, and the finalizer could produce a verdict about a
    directory the agent never wrote to (hostile review 2026-09-16, I4). The
    loop records what it used; this returns that, or the caller's fallback for
    a run that never reached the guard.
    """
    for candidate in (getattr(session, WORKSPACE_ATTR, None), fallback):
        if candidate:
            return str(candidate)
    return None


def record_deliverable_verdicts(run: Any, session: Any, workspace: str | Path | None) -> None:
    """The deliverable verdict for one finished run, at whatever rung is configured.

    The complement of the completion contract: that one asks whether the
    agent's CLAIMS are backed by trace evidence, this one asks whether the
    artifact the TASK named exists and is the shape it described. A run passes
    the first and fails the second by doing the work correctly and saving it
    somewhere else, or to the right place in the wrong shape — measured
    2026-08-26 as -0.87 of a -1.04 competitive gap in which 7 of 10 tasks were
    at parity, and again 2026-09-16 as three tasks at 0 against a competitor's
    86 / 91 / 49.

    ONE verdict, not two. The contract's `PathItem` asks exactly the question
    the original path-only check asked, over the same extracted paths — but
    confined to the workspace, where the original resolved the task's own
    string against the whole filesystem. Running both would report the same
    absent file twice and disagree about it whenever the run's workspace is not
    the directory the task's absolute path names.

    The loop's re-ask (``loop_guards.reask_for_wrong_deliverable_shape``) is
    the half that can change an outcome; this is the half that stops a
    wrong-shaped run from reporting success. Three measured runs reported
    `completed` with the file present, the header wrong, and every grader
    criterion at 0. A `completed` row for that run is a false record, and the
    fleet's own dashboards read it.
    """
    from robothor.engine.feature_flags import deliverable_contract_mode
    from robothor.engine.models import RunStatus

    mode = deliverable_contract_mode()
    if mode == "off":
        return
    try:
        from robothor.engine.deliverable_contract import contract_report_for_run

        # ALWAYS a fresh read. This used to prefer the verdict the loop stashed
        # when it re-asked, on the reasoning that reading twice was two answers
        # to one question. It is not: the re-ask exists so the agent can FIX the
        # file, and between the loop's read and this one it may have done
        # exactly that. Preferring the stash meant every run that complied with
        # the re-ask was still recorded `failed`, with a `blocked` row and an
        # operator alert — the one success path this feature exists to create
        # was unreachable (hostile review 2026-09-16, C1).
        report = contract_report_for_run(run, session, resolve_workspace(session, workspace))
    except Exception as exc:  # noqa: BLE001 — never block finalization
        logger.debug("deliverable shape check raised: %s", exc)
        return
    # None means the task stated no contract, which is most runs. Logging a
    # vacuous pass on every one of them would bury the real verdicts in exactly
    # the way the alert digest already does.
    if report is None or report.satisfied:
        return
    # A run that DECLINED is not a run that fell short. Recorded, never
    # blocked, never alerted — see `reads_as_a_refusal`.
    if reads_as_a_refusal(getattr(run, "output_text", None)):
        refused = (
            "Deliverable contract not satisfied, and the run refused the task:\n" + report.message
        )
        _log_event(run, refused, "observe")
        logger.info("deliverable contract: run %s refused the task — %s", run.id, refused[:500])
        return
    summary = "Deliverable contract not satisfied:\n" + report.message
    _log_event(run, summary, mode)
    if mode != "enforce":
        logger.warning("deliverable contract %s: run %s — %s", mode, run.id, summary[:500])
        if mode == "alert":
            _alert(run, summary)
        return
    # Only a run that would otherwise claim success is turned over. A run that
    # already failed keeps the error it actually hit; burying that under this
    # verdict would lose the cause, and the cause is what an operator opens the
    # row for.
    if run.status == RunStatus.COMPLETED:
        run.status = RunStatus.FAILED
    run.error_message = "\n".join(x for x in (run.error_message, summary) if x)[:4000]
    logger.warning("deliverable contract enforce: run %s failed — %s", run.id, summary[:500])
    _alert(run, summary)


def _log_event(run: Any, reason: str, mode: str) -> None:
    with contextlib.suppress(Exception):
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run.id,
            guardrail_name="deliverable_contract",
            action="blocked" if mode == "enforce" else "observed",
            reason=reason[:500],
            mode=mode,
        )


def _alert(run: Any, reason: str) -> None:
    with contextlib.suppress(Exception):
        from robothor.engine.feature_flags import notify_guardrail_alert

        notify_guardrail_alert(
            guardrail_name="deliverable_contract",
            agent_id=run.agent_id,
            reason=reason[:500],
            tenant_id=getattr(run, "tenant_id", "") or "",
        )
