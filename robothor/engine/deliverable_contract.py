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

MEASURED SCOPE, and why the flag stays off (2026-08-27). Probed against
production: of **4,000 crm_tasks from the last 60 days, ZERO name an
explicit output path**. Wiring this to ``crm_tasks`` and promoting the flag
would therefore ship a control that can never fire — a guard on an empty
table, which this instance has now done six times
(``feedback-probe-dont-trust-silence``). It stays ``off`` until it has a
source of task text that actually carries contracts.

Where contracts DO exist is prompt-borne task specs — the benchmark harness
states "save them to /tmp_workspace/results/2022.tsv" — and a run's prompt
is not persisted (``AgentRun`` keeps ``user_prompt_chars``, a count, not the
text). Closing that is the follow-on: give the finalizer a task-text source
that includes the originating prompt, then probe again before promoting.

The module is deliberately usable without the finalizer: ``required_deliverables``
and ``check_deliverables`` are pure and importable by any caller that already
holds the task wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Verbs that introduce an OUTPUT. "read from", "load", "open" deliberately
#: absent: naming an input file is not promising to create it.
_OUTPUT_VERB = r"(?:save|write|store|export|output|put|place|dump|emit)"

#: A concrete local path: at least one path-ish character and a real
#: extension. Bare extensions (".tsv") and URLs are excluded by construction
#: -- the negative lookbehind keeps us off "://host/results/x.tsv".
_PATH = r"(?<![\w:/.])((?:/|\.{1,2}/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z][\w]{0,7})"

#: What may sit between the preposition and the path. Real prompts do not put
#: them adjacent: WildClawBench task_4 writes
#:
#:     ...and save them to:
#:
#:     - `/tmp_workspace/results/2022.tsv`
#:
#: an optional colon, a line break, a markdown bullet and backticks. Requiring
#: the path on the SAME line is why that task's contract went unseen while the
#: run reported "completed" having written nothing — the failure this module
#: was built for, and the one it could not see.
_GAP = r"(?:[ \t]*:)?[ \t]*(?:\r?\n[ \t]*)*(?:[-*+][ \t]+)?`?"

_CONTRACT_PATTERNS = [
    # "save them to X", "write the output to X", "export results into X",
    # including "save them to:" followed by a bulleted path on the next line.
    re.compile(rf"{_OUTPUT_VERB}\b[^.\n]{{0,60}}?\b(?:to|into|in|at)\b{_GAP}{_PATH}", re.IGNORECASE),
    # "save as X", "output as X"
    re.compile(rf"{_OUTPUT_VERB}\b[^.\n]{{0,30}}?\bas\b{_GAP}{_PATH}", re.IGNORECASE),
]

#: Anything inside one of these is a URL, not a local deliverable.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)


def required_deliverables(task_text: str | None) -> list[str]:
    """Paths the task explicitly asks the run to produce, in order of mention.

    Returns ``[]`` whenever the task does not name a concrete output path --
    which is most tasks. Silence here is the safe default: this control only
    speaks when the task was unambiguous about where its result belongs.
    """
    if not task_text:
        return []
    # Blank out URLs so a path inside one can never be mistaken for a local
    # deliverable, while keeping offsets stable for everything else.
    scrubbed = _URL_RE.sub(lambda m: " " * len(m.group(0)), task_text)

    found: list[str] = []
    for pattern in _CONTRACT_PATTERNS:
        for match in pattern.finditer(scrubbed):
            path = match.group(1)
            if path not in found:
                found.append(path)
    return found


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


def task_text_for_run(run: object, session: object = None) -> str:
    """The task wording a deliverable contract can be read from.

    Prefers the originating ``crm_task`` (title + objective): a delegated task
    is where an explicit output path is most often written down. Falls back to
    the run's originating MESSAGE, because most runs have no crm_task at all —
    every benchmark task included, which is precisely the population this
    contract was built for and could not see. Returns ``""`` when neither is
    available; the contract then requires nothing, which is the safe direction.
    """
    task_id = getattr(run, "task_id", None)
    if not task_id:
        return str(getattr(session, "originating_message", "") or "")
    try:
        from robothor.crm import dal

        task = dal.get_task(task_id, tenant_id=getattr(run, "tenant_id", None) or "default")
    except Exception:  # noqa: BLE001 — a contract check must never break a run
        return ""
    if not task:
        return str(getattr(session, "originating_message", "") or "")
    return " ".join(str(task.get(k) or "") for k in ("title", "objective", "next_action")).strip()


def check_run_deliverables(run: object, session: object = None) -> DeliverableReport | None:
    """Verdict for one run, or None when the task named no deliverable.

    None is the common case and is deliberately distinct from "satisfied":
    the caller should log nothing at all rather than record a vacuous pass on
    every run in the fleet.
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
