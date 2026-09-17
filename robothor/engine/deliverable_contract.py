"""Did the run produce the artifact the TASK named?

``run_verification.verify_run`` answers a different question: does the
agent's final message make claims the trace supports? That catches an agent
saying it wrote a file when no write happened. It cannot catch an agent that
genuinely wrote a file -- just not the one that was asked for.

2026-08-26, WildClaw task_4. The spec said "save them to
``/tmp_workspace/results/2022.tsv``". The agent did the research correctly,
verifying 7 of 9 author homepages with live HTTP 200s, then wrote
``/tmp_workspace/results/summary.md``. Every criterion scored 0.00,
``output_exists`` included, after 3.4M tokens. That one task carries -0.87
of a -1.04 competitive gap in which 7 of 10 tasks are at parity: not a
diffuse capability deficit, a contract failure.

Design constraints, learned the hard way:

* **General, not bench-shaped.** Fixing this by editing a benchmark agent's
  prompt would be teaching to the test, the error recorded in
  ``peak-performance-campaign-2026-08-21`` where a calibration skill taught
  agents to widen a regex until it matched.
* **Conservative extraction.** A false positive nags an agent about a file
  that was never a deliverable, and a control that cries wolf gets muted.
  Only an explicit output verb pointed at a concrete, local, extensioned
  path counts. Reads, prose, bare extensions and URLs do not.
* **Existence is not enough.** An empty file at the right path is a touched
  path, not a produced deliverable.

MEASURED SCOPE (2026-08-27). Probed against production: of **4,000 crm_tasks
from the last 60 days, ZERO name an explicit output path**. Wiring this to
``crm_tasks`` alone would have shipped a control that can never fire — a
guard on an empty table, which this instance has now done six times
(``feedback-probe-dont-trust-silence``). Where contracts DO exist is
prompt-borne task specs.

THE SOURCE, closed 2026-09-16 (migration 123). A run's prompt used not to be
persisted at all — ``AgentRun`` kept ``user_prompt_chars``, a count, not the
text — so at finalization the control had nothing to read. ``agent_runs``
now carries ``task_text``: the originating prompt, redacted through the same
door as chat history and capped, written at session start. See
``task_text_for_column`` and ``task_text_for_run``.

THE SHAPE, added the same day. See the "Shape contracts" section below: a
path-only contract cannot see a file written to the right place with the
wrong columns, the wrong fields or renamed headings, which is how three
benchmark tasks scored 0 while a competitor scored 86 / 91 / 49.

The module is deliberately usable without the finalizer: ``required_deliverables``,
``check_deliverables``, ``extract_contract`` and ``check_contract`` are pure
and importable by any caller that already holds the task wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# The seam, drawn 2026-09-16 (hostile review I6). This module is the façade:
# every import site keeps working, and the two halves that share nothing but
# the item definitions live apart where the size ratchet can see them.
from robothor.engine.deliverable_check import (
    check_contract,
)
from robothor.engine.deliverable_extract import (
    extract_contract,
    required_deliverables,
)
from robothor.engine.deliverable_items import (
    STATUS_MISMATCH,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_UNCHECKED,
    ContractItem,
    ContractReport,
    DeliverableContract,
    ExactSetItem,
    HeaderItem,
    ItemFinding,
    JsonFieldsItem,
    PathItem,
    PatternItem,
    SectionsItem,
    SortItem,
)
from robothor.engine.deliverable_items import (
    listed as _listed,
)

__all__ = [
    "STATUS_MISMATCH",
    "STATUS_MISSING",
    "STATUS_OK",
    "STATUS_UNCHECKED",
    "STICKY_BLOCK_MAX_CHARS",
    "STICKY_MARKER",
    "TASK_TEXT_MAX_CHARS",
    "ContractItem",
    "ContractReport",
    "DeliverableContract",
    "DeliverableReport",
    "ExactSetItem",
    "HeaderItem",
    "ItemFinding",
    "JsonFieldsItem",
    "PathItem",
    "PatternItem",
    "SectionsItem",
    "SortItem",
    "check_contract",
    "check_deliverables",
    "check_run_deliverables",
    "contract_checkin_note",
    "contract_reask_note",
    "contract_report_for_run",
    "contract_sticky_block",
    "deliverable_nudge",
    "extract_contract",
    "required_deliverables",
    "task_text_for_column",
    "task_text_for_run",
]


@dataclass(frozen=True)
class DeliverableReport:
    """What the contract asked for, and what is actually on disk."""

    required: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return not self.missing

    @property
    def message(self) -> str:
        """Operator- and agent-readable. Names the remedy, not just the fault.

        A message that says only "verification failed" is the same defect as
        a page that says only "unit FAILED" -- true, unactionable, ignored.
        """
        if self.satisfied:
            return ""
        listed = ", ".join(self.missing)
        return (
            f"The task asked for {listed}, which does not exist or is empty. "
            f"Produce exactly that path before finishing — work saved "
            f"elsewhere does not satisfy the request."
        )


def check_deliverables(required: list[str]) -> DeliverableReport:
    """Which required paths are absent or empty.

    Empty counts as missing on purpose: on the task this was built for, an
    agent that creates the right filename and writes nothing into it has not
    delivered, and scoring that as success would teach exactly that shortcut.
    """
    missing: list[str] = []
    for raw in required:
        try:
            p = Path(raw)
            if not p.is_file() or p.stat().st_size == 0:
                missing.append(raw)
        except OSError:
            missing.append(raw)
    return DeliverableReport(required=list(required), missing=missing)


#: What `agent_runs.task_text` will hold. A task spec that states an output
#: contract is a page or two; anything past this is pasted data, and the
#: column is not a document store.
TASK_TEXT_MAX_CHARS = 32_768

#: Marker for the elision, matching `tracking._truncate`'s idiom.
_ELISION = "\n\n[... truncated {count} chars ...]\n\n"


def task_text_for_column(user_message: str | None) -> str | None:
    """The originating prompt, as `agent_runs.task_text` should hold it.

    Redacted first and truncated second, in that order and not the other: the
    cap applied first could split a credential across the boundary and leave a
    prefix the redactor no longer recognises.

    The elision takes the MIDDLE, not the tail. A spec states its output
    contract in an "Output Requirements" section at the end at least as often
    as in its opening sentence, and a cap that always eats the end would drop
    exactly the half this column exists to carry.
    """
    if not user_message:
        return None
    from robothor.secrets.redaction import redact

    text = redact(str(user_message))
    if len(text) <= TASK_TEXT_MAX_CHARS:
        return text
    marker = _ELISION.format(count=len(text) - TASK_TEXT_MAX_CHARS)
    half = (TASK_TEXT_MAX_CHARS - len(marker)) // 2
    return text[:half] + marker + text[-half:]


def task_text_for_run(run: object, session: object = None) -> str:
    """The task wording a deliverable contract can be read from.

    Three sources, in the order of how much each is trusted to still be there
    when the verdict is computed:

    1. ``run.task_text`` — the originating prompt, persisted at run start
       (migration 123). The only one that survives compaction, a resumed run
       and the finalizer, which runs after the loop has ended.
    2. the live session's originating message, for a run in flight.
    3. the originating ``crm_task`` (title + objective + next action).

    The crm_task is last, not first, on measured evidence: of 4,000 crm_tasks
    over 60 days on the first production instance, ZERO named an explicit
    output path. The contracts are in prompt-borne task specs, and reading the
    task row first meant a run WITH a task row could never see its own spec.

    On top of whichever one answers: the body of every SKILL this run loaded.
    Measured 2026-09-16 — one benchmark task states its output path only inside
    its skill, so prompt-only extraction returned an empty contract for exactly
    the run that had been handed one. Appended rather than preferred: a skill is
    additional instruction, never a replacement for what the operator asked.

    Returns ``""`` when none is available; the contract then requires nothing.
    """
    from robothor.engine.skill_contract import loaded_skill_text

    return _join(_prompt_text(run, session), loaded_skill_text(session))


def _join(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part).strip()


def _prompt_text(run: object, session: object = None) -> str:
    """The three prompt-borne sources, in order of how much each is trusted."""
    persisted = str(getattr(run, "task_text", "") or "")
    if persisted:
        return persisted
    live = str(getattr(session, "originating_message", "") or "")
    if live:
        return live
    task_id = getattr(run, "task_id", None)
    if not task_id:
        return ""
    try:
        from robothor.crm import dal

        task = dal.get_task(task_id, tenant_id=getattr(run, "tenant_id", None) or "default")
    except Exception:  # noqa: BLE001 — a contract check must never break a run
        return ""
    if not task:
        return ""
    return " ".join(str(task.get(k) or "") for k in ("title", "objective", "next_action")).strip()


def check_run_deliverables(run: object, session: object = None) -> DeliverableReport | None:
    """Path-only verdict for one run, or None when the task named no deliverable.

    None is the common case and is deliberately distinct from "satisfied":
    the caller should log nothing at all rather than record a vacuous pass on
    every run in the fleet.

    The finalizer no longer uses this — it reads the full contract, whose
    ``PathItem`` asks the same question over the same extracted paths but
    confined to the run's workspace, where this resolves the task's own string
    against the whole filesystem. Kept because it is pure, importable, and the
    in-loop nudge is still built on the same pair of functions.
    """
    required = required_deliverables(task_text_for_run(run, session))
    if not required:
        return None
    return check_deliverables(required)


#: One nudge per run. An unbounded "you are not done" is a loop, and the agent
#: may have a good reason the artifact is absent that it cannot fix by trying
#: again.
MAX_DELIVERABLE_NUDGES = 1


def deliverable_nudge(session: object, nudges_used: int = 0) -> str | None:
    """What to tell an agent that stopped short of the artifact it was asked for.

    Returns None when there is nothing to say — no session, no named
    deliverable (most runs), the artifact exists, or the nudge budget is spent.

    The contract's verdict already lands in `run_finalizer`, but that runs AFTER
    the loop: it can record a missing artifact, never prevent one. This is the
    same verdict delivered while iterations remain. WildClawBench task_4 is the
    case — 333 requests, 704 seconds, status "completed", nothing written — and
    a post-hoc verdict there turns a 0.0 into a documented 0.0.
    """
    if session is None or nudges_used >= MAX_DELIVERABLE_NUDGES:
        return None
    text = str(getattr(session, "originating_message", "") or "")
    if not text:
        return None
    required = required_deliverables(text)
    if not required:
        return None
    report = check_deliverables(required)
    if report.satisfied:
        return None
    missing = ", ".join(report.missing)
    return (
        "[SYSTEM] You have stopped without producing the deliverable this task "
        f"named: {missing}. It does not exist, or is empty. If the work is done, "
        "write it to that exact path now. If you cannot, say plainly what "
        "prevented it — do not end as though the artifact exists."
    )


# ─── Where it acts ────────────────────────────────────────────────────


def contract_report_for_run(
    run: object, session: object = None, workspace: str | Path | None = None
) -> ContractReport | None:
    """The shape verdict for one run, or None when the task stated no shape.

    None is the common case and is deliberately distinct from "satisfied": a
    vacuous pass recorded on every run in the fleet buries the real verdicts,
    which is what the alert digest already does to itself.
    """
    if not workspace:
        return None
    contract = extract_contract(task_text_for_run(run, session))
    if not contract:
        return None
    report = check_contract(contract, workspace)
    return report if report.findings else None


#: Two, because a note that quotes the whole spec back is the spec, and the
#: agent already has that.
_MAX_QUOTED_EVIDENCE = 2


def _quoted_evidence(report: ContractReport) -> str:
    seen: list[str] = []
    for finding in report.failures:
        evidence = getattr(finding.item, "evidence", "")
        if evidence and evidence not in seen:
            seen.append(evidence)
        if len(seen) >= _MAX_QUOTED_EVIDENCE:
            break
    return "\n".join(f'  the task said: "{e}"' for e in seen)


#: The marker the sticky block carries, so the engine can tell whether a
#: conversation already has one rather than stacking a second.
STICKY_MARKER = "[TASK OUTPUT CONTRACT]"

#: Ceiling on the sticky block. It is re-sent on every compaction of every long
#: run: a block that grows with the task would be a second context problem,
#: solved by the thing that was meant to fix the first one.
STICKY_BLOCK_MAX_CHARS = 2_000


def _sticky_line(item: ContractItem) -> str:
    """One item of the contract, in the task's own terms."""
    if isinstance(item, PathItem):
        return f"- write `{item.path}` (it must exist and not be empty)"
    if isinstance(item, ExactSetItem):
        rule = "exactly these and nothing else" if item.forbid_extra else "at least these"
        return f"- `{item.directory}` must contain {rule}: {_listed(item.names)}"
    if isinstance(item, HeaderItem):
        delimiter = "tab" if item.delimiter == "\t" else f"`{item.delimiter}`"
        return f"- `{item.path}` first row, {delimiter}-separated, exactly: `{item.required_text}`"
    if isinstance(item, JsonFieldsItem):
        return (
            f"- `{item.path}` is a JSON {item.container}; each object carries exactly "
            f"{_listed(item.fields)}"
        )
    if isinstance(item, SectionsItem):
        return f"- `{item.path}` section headings, not to be renamed: {_listed(item.headings)}"
    if isinstance(item, SortItem):
        return f"- `{item.path}` rows sorted ascending by {_listed(item.keys)}"
    return ""  # pragma: no cover — the union is closed


def contract_sticky_block(task_text: str | None) -> str | None:
    """The output contract, rendered small enough to re-send after every compaction.

    MEASURED 2026-09-16: four benchmark runs whose transcript began with a
    compaction summary scored a mean of 0.016 against 0.450 for the six that
    never compacted. The required header string appears zero times in the worst
    one's whole context — the spec had been summarised away, and the agent then
    invented its own columns.

    ``protected_prefix_len`` is the structural half of the fix and this is the
    restorative half: even on a run that has compacted twice, the exact path,
    header, fields and headings are still in front of the model. It is the
    task's OWN words, and says so — an agent that cannot tell an engine
    reminder from the task itself will argue with one of them.

    Returns None whenever the task stated no contract, which is most tasks.
    """
    contract = extract_contract(task_text)
    if not contract:
        return None
    lines = [_sticky_line(item) for item in contract.items]
    body = "\n".join(line for line in lines if line)
    if not body:
        return None
    header = (
        f"{STICKY_MARKER} These are the TASK's own requirements for its output, "
        "repeated here because earlier turns may have been summarised away. They "
        "are exact. Check the workspace against them before you finish.\n"
    )
    block = header + body
    if len(block) <= STICKY_BLOCK_MAX_CHARS:
        return block
    kept: list[str] = []
    budget = STICKY_BLOCK_MAX_CHARS - len(header) - 40
    for line in lines:
        if not line:
            continue
        if budget - len(line) - 1 < 0:
            break
        kept.append(line)
        budget -= len(line) + 1
    return header + "\n".join(kept) + f"\n- (+{len(lines) - len(kept)} further requirements)"


def contract_checkin_note(task_text: str | None, workspace: str | Path | None) -> str | None:
    """The deliverable check-in, as a COMPARISON rather than a question.

    "Are you making progress" is a question an agent answers yes to. What is on
    disk, and whether its shape is the one the task stated, is checkable — and
    it is what the graders and the operator actually read. Returns None when
    the task stated no shape or the workspace already matches it, which is the
    common case and has to stay silent.
    """
    if not workspace:
        return None
    contract = extract_contract(task_text)
    if not contract:
        return None
    report = check_contract(contract, workspace)
    if report.satisfied and not report.unchecked:
        return None
    if report.satisfied:
        # Nothing is wrong — but something could not be read, and the agent is
        # the only party that can still do anything about it while the run is
        # alive (re-review 2026-09-16, R3).
        return (
            "[SYSTEM] Deliverable check: one of this task's outputs is too large for "
            "the engine to verify, so nothing here confirms it is right.\n"
            + report.message
            + "\nCheck it yourself against the task's own wording before you finish."
        )
    return (
        "[SYSTEM] Deliverable check: the workspace does not yet match the output "
        "contract this task stated.\n"
        + report.message
        + "\nThese are exact requirements, not suggestions. Fix them against the "
        "task's own wording before you finish."
    )


def contract_reask_note(report: ContractReport) -> str | None:
    """One last ask, with the report, before the run is failed honestly.

    Bounded at one for the reason ``MAX_DELIVERABLE_NUDGES`` is: an unbounded
    "you are not done" is a loop, and the agent may have a reason the shape is
    wrong that trying again will not fix. The last sentence exists because some
    tasks SHOULD be refused — a run that is right to produce nothing must be
    able to say so rather than be pushed into fabricating the artifact.
    """
    if report is None or report.satisfied:
        return None
    quoted = _quoted_evidence(report)
    return (
        "[SYSTEM] You have stopped, but the workspace does not match the output "
        "contract this task stated:\n"
        + report.message
        + (f"\n{quoted}" if quoted else "")
        + "\nCorrect this now — the exact path, the exact header, the exact fields "
        "and the exact headings, as the task wrote them. If you cannot, or if this "
        "is a task you should not complete, say plainly why instead: do not end as "
        "though the deliverable is right, and do not invent content to fill it."
    )
