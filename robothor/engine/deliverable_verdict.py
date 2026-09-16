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


#: An agent DECLINING — not an agent reporting that something did not work.
#:
#: Measured by re-review 2026-09-16 (R2): reading the closing text for
#: first-person inability (`I can't`, `I cannot`, `I won't`) excused 7 of 9
#: ordinary failure summaries. The engine's own hard-abort message asks the
#: agent to end with "What failed and why", which reliably produces exactly
#: that wording — so the runs most likely to be holding a wrong-shaped
#: deliverable were also the runs most likely to be let off.
#:
#: So: an explicit declining verb. Bare `cannot` is gone.
_DECLINE_RE = re.compile(
    r"\bI\s+(?:will\s+not|won'?t|refuse|decline|should\s?n'?t|must\s+not)\b"
    r"|\bI'?m\s+(?:declining|refusing|not\s+going\s+to)\b"
    r"|\bI\s+am\s+(?:declining|refusing|not\s+going\s+to)\b"
    # `can't` / `cannot` / `unable to` are the commonest way an aligned model
    # declines — "I can't produce this content" is the canonical Safety
    # refusal — so excluding them failed a correct refusal with a `blocked`
    # row and an operator alert, and would have made the promotion gate's own
    # refusal audit read as a false block (final re-check 2026-09-16, F1).
    #
    # Admitting them is safe ONLY because the reason clause below and the
    # nothing-was-attempted condition still stand: "I cannot reach the site"
    # has no reason of the right kind, and "I cannot guarantee the abstracts"
    # comes with a file on disk. Both were measured.
    r"|\bI\s+(?:can'?t|cannot)\b"
    r"|\bI\s+(?:am|'?m)\s+(?:un|not\s+)able\s+to\b",
    re.IGNORECASE,
)

#: …and a reason about the TASK, not about the tooling. "I won't have time to
#: finish the renames" uses a declining verb about a schedule; a refusal is
#: about what the task asks for.
_DECLINE_REASON_RE = re.compile(
    # STEMS, matched as prefixes: "dehumanis" has to reach "dehumanising", and
    # a trailing `\b` there is the same mistake that let `e.g.` through the
    # suppressor — a boundary asserted where the word carries on.
    r"\b(?:harm|harass|dehumanis|dehumaniz|misinform|disinform|mislead|defam|"
    r"hateful|hate\s+speech|abusiv|slur|unethical|illegal|discriminat|"
    r"demean|degrad|target(?:s|ing)?\s+a\s+(?:group|person|individual))"
    # …and whole phrases.
    r"|\b(?:false\s+(?:claim|health|information|statement)|"
    r"against\s+(?:policy|my\s+guidelines|the\s+guidelines)|policy|"
    r"private\s+data|personal\s+data|credentials?|"
    r"told\s+not\s+to|instructed\s+not\s+to|asked\s+not\s+to|"
    r"should\s+not\s+(?:be\s+)?(?:produced?|published?|written|created?))\b",
    re.IGNORECASE,
)

#: Only the closing words count. A refusal is how a run ENDS; the same phrase
#: mid-transcript is usually the agent narrating an obstacle it worked around.
_REFUSAL_TAIL_CHARS = 1_500


def reads_as_a_refusal(output_text: str | None, *, all_missing: bool) -> bool:
    """Did the run DECLINE the task, rather than fail to finish it?

    Two conditions, both required, because either alone is gameable.

    **Declining, with a reason about the task.** An explicit declining verb and
    a stated reason of the kind a refusal actually has — harmful, misleading,
    against policy, told not to. "I could not reach the site" is a report.

    **Nothing was attempted.** A refusal produces no file at all; a shortfall
    usually produces a wrong one. So every failing item must be `missing`: a
    `mismatch` means the agent wrote something, and whatever it said about
    itself, it did not decline.

    Narrow on purpose, and narrower than it was. Widening this is how the gate
    becomes an exit — and the wider version measurably was one.
    """
    if not output_text or not all_missing:
        return False
    tail = str(output_text)[-_REFUSAL_TAIL_CHARS:]
    return bool(_DECLINE_RE.search(tail)) and bool(_DECLINE_REASON_RE.search(tail))


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
    from robothor.engine.deliverable_items import STATUS_MISSING
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
    if report is None:
        return
    if report.satisfied:
        # …but a deliverable nobody could verify is not a vacuous pass. It is
        # the one case where `satisfied` is True and an operator still needs to
        # know, and it was invisible on every surface (re-review 2026-09-16,
        # R3). Recorded, never blocked, never alerted: "I did not check" is not
        # a fault of the run.
        if report.unchecked:
            note = "Deliverable contract partly unverified:\n" + report.message
            _log_event(run, note, "unchecked")
            logger.warning("deliverable contract: run %s — %s", run.id, note[:500])
        return
    # A run that DECLINED is not a run that fell short. Recorded, never
    # blocked, never alerted — see `reads_as_a_refusal`.
    if reads_as_a_refusal(
        getattr(run, "output_text", None),
        all_missing=all(f.status == STATUS_MISSING for f in report.failures),
    ):
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

        # `unchecked` is its own action, not an `observed` breach: the evidence
        # query an operator runs to read this control must be able to separate
        # "we looked and it was wrong" from "we could not look".
        action = mode if mode == "unchecked" else ("blocked" if mode == "enforce" else "observed")
        log_guardrail_event(
            run_id=run.id,
            guardrail_name="deliverable_contract",
            action=action,
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
