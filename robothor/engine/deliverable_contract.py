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

import json
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
    re.compile(
        rf"{_OUTPUT_VERB}\b[^.\n]{{0,60}}?\b(?:to|into|in|at)\b{_GAP}{_PATH}", re.IGNORECASE
    ),
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


# ─── Shape contracts ──────────────────────────────────────────────────
#
# MEASURED 2026-09-16. Three Productivity tasks of the public agent benchmark,
# run like-for-like against a competing harness — same tasks, same model, the
# authors' own graders. The competitor scored 86 / 91 / 49; this engine scored
# 0 / 0 / 0. In every one the agent did the research and then ignored the
# output contract:
#
#   * the spec named six TSV columns in an exact order; we wrote five of our
#     own and dropped two required fields. `output_exists` scored 1 and every
#     other criterion scored 0.
#   * the spec named an exact set of outputs under a results directory; we
#     wrote one file of our own naming into the workspace root and left the
#     results directory empty.
#   * the spec gave the section headings verbatim and said not to rename them;
#     we wrote the right path with our own headings.
#
# A path-only contract sees a file at the right place and says nothing. The
# question it cannot ask is the one the grader asks: is this the shape the task
# described. So the contract grows items — a header, a field set, a heading
# list, a sort, an exact output set — and each is extracted ONLY from language
# that states the requirement explicitly.
#
# The docstring's constraints still bind, and bind harder here. A false
# positive now fails a run rather than merely nagging, so every extractor
# demands an explicit anchor ("use exactly the following header", "must contain
# exactly these fields", "do not rename these section headings"), and anything
# it cannot enumerate it declines to enforce: a bullet list with one prose
# entry yields a set that cannot forbid extras, and a sort with no matching
# header yields no item at all.

#: Enough of a file to read a header, a heading list or a manifest without
#: pulling an arbitrary artifact into memory.
_MAX_READ_BYTES = 2_000_000

#: Most names a single reason will list before it summarises the rest. A wall
#: of names is the same defect as no names at all.
_MAX_LISTED = 10


@dataclass(frozen=True)
class PathItem:
    """The original contract: this path must exist and be non-empty."""

    path: str
    evidence: str = ""


@dataclass(frozen=True)
class ExactSetItem:
    """These names, under this directory.

    ``forbid_extra`` only when the spec both said so AND the list could be
    enumerated. "one or more source ``.tex`` files" is a requirement we cannot
    turn into a name, and a set with an unknown member cannot call anything an
    intruder.
    """

    directory: str
    names: tuple[str, ...]
    forbid_extra: bool = False
    evidence: str = ""


@dataclass(frozen=True)
class HeaderItem:
    """A delimited file's first row.

    ``raw`` is the spec's header line with its whitespace collapsed, and it is
    what equality is decided on. ``columns`` is a best-effort split of that
    line, used to name the fields a file dropped.

    The split has to be best-effort because a markdown spec cannot render a
    tab: the measured task wrote its six-column header as runs of one to four
    spaces, and a column name may itself contain a space (``Author links``).
    No split of that line is reliable — but collapsing BOTH sides to
    single-spaced text compares them exactly, whatever whitespace each used,
    and a file whose header is right passes however the spec drew it.
    """

    path: str
    columns: tuple[str, ...]
    delimiter: str = "\t"
    raw: str = ""
    evidence: str = ""

    @property
    def required_text(self) -> str:
        return " ".join((self.raw or " ".join(self.columns)).split())


@dataclass(frozen=True)
class JsonFieldsItem:
    """A JSON document's field set, and whether it is an array or an object."""

    path: str
    fields: tuple[str, ...]
    container: str = "array"
    evidence: str = ""


@dataclass(frozen=True)
class SectionsItem:
    """Headings a document must carry, under exactly these names."""

    path: str
    headings: tuple[str, ...]
    evidence: str = ""


@dataclass(frozen=True)
class SortItem:
    """The columns a delimited file's rows must ascend by."""

    path: str
    keys: tuple[str, ...]
    evidence: str = ""


ContractItem = PathItem | ExactSetItem | HeaderItem | JsonFieldsItem | SectionsItem | SortItem


@dataclass(frozen=True)
class DeliverableContract:
    """Everything one task said about the shape of its output."""

    items: tuple[ContractItem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)


STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_MISMATCH = "mismatch"


@dataclass(frozen=True)
class ItemFinding:
    """One contract item, and what the workspace says about it."""

    item: ContractItem
    status: str
    reason: str = ""


@dataclass(frozen=True)
class ContractReport:
    """The workspace read against the contract, item by item."""

    findings: tuple[ItemFinding, ...] = ()

    @property
    def failures(self) -> tuple[ItemFinding, ...]:
        return tuple(f for f in self.findings if f.status != STATUS_OK)

    @property
    def satisfied(self) -> bool:
        return not self.failures

    @property
    def message(self) -> str:
        """One line per fault, each naming the remedy rather than the verdict.

        A report that says only "contract not satisfied" is the same defect as
        a status page that says only "unit FAILED": true, unactionable, ignored.
        """
        return "\n".join(f.reason for f in self.failures if f.reason)


# ─── Extraction ───────────────────────────────────────────────────────

#: Every local path in a span of text, same shape as ``_PATH`` above.
_ANY_PATH_RE = re.compile(_PATH)

#: A directory: path-ish, trailing slash, no extension required.
_DIR = r"((?:/|\.{1,2}/)?(?:[\w.\-]+/)+)"

#: ```lang\n…\n``` — the block, its language and where it starts.
_FENCE_RE = re.compile(
    r"^```[ \t]*([A-Za-z0-9_+.\-]*)[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL
)

#: "You must create exactly the following outputs under `results/`:"
#: The verb list is the OUTPUT verbs plus create/produce/generate: naming an
#: input directory is not promising to fill it.
#: Two orders, because specs use both: "…the following outputs under `x/`:"
#: and "…into `x/` with the following files:". Missing the second one left a
#: real task's heading contract attached to a bare filename with no directory,
#: which a checker would have looked for in the workspace root.
_OUTPUT_SET_RE = re.compile(
    rf"(?:create|produce|generate|{_OUTPUT_VERB})\b[^.\n]{{0,40}}?"
    rf"(?:"
    rf"\b(exactly\s+)?the\s+following\s+(?:outputs?|files?|deliverables?)\s+"
    rf"(?:under|in|inside|into)\s+`?{_DIR}`?\s*:"
    rf"|"
    rf"\b(?:under|in|inside|into|to)\s+`?{_DIR}`?\s+with\s+(exactly\s+)?"
    rf"the\s+following\s+(?:outputs?|files?|deliverables?)\s*:"
    rf")",
    re.IGNORECASE,
)

#: "Do not create any other files or directories" — the other half of an exact
#: set, and often the only half a spec states.
_NO_OTHERS_RE = re.compile(
    r"do\s+not\s+(?:create|add|leave|include)\s+any\s+other\s+(?:files?|outputs?|directories)",
    re.IGNORECASE,
)

#: "use exactly the following header", "the header must be exactly".
_HEADER_ANCHOR_RE = re.compile(
    r"(?:use|using|with|contain|include|write)\s+exactly\s+the\s+following\s+header"
    r"|the\s+header\s+(?:row\s+)?must\s+be\s+exactly"
    r"|exactly\s+the\s+following\s+header",
    re.IGNORECASE,
)

#: "The columns are: A, B, C" / "columns: A, B, C". Deliberately narrow — the
#: word has to open the clause and end in a colon, or every sentence about a
#: table matches.
_COLUMNS_RE = re.compile(
    r"\b(?:the\s+)?columns\s*(?:are|must\s+be)?\s*:\s*([^\n.]{3,200})",
    re.IGNORECASE,
)

#: "Each item must contain exactly these fields".
_JSON_FIELDS_ANCHOR_RE = re.compile(
    r"must\s+contain\s+exactly\s+(?:these|the\s+following)\s+fields",
    re.IGNORECASE,
)

#: "using exactly the following structure", "do not rename these section headings".
_SECTIONS_ANCHOR_RE = re.compile(
    r"exactly\s+the\s+following\s+structure"
    r"|do\s+not\s+rename\s+(?:these|the)\s+section\s+headings",
    re.IGNORECASE,
)

#: "sorted by `X` ascending, then by `Y` ascending". Bounded repetition: this
#: runs over untrusted task text and must not backtrack.
_SORT_RE = re.compile(
    r"\bsorted\s+by\s+`?([\w ]{1,40}?)`?\s+(?:ascending|asc)\b"
    r"(?:\s*,?\s*(?:and\s+)?then\s+(?:by\s+)?`?([\w ]{1,40}?)`?\s+(?:ascending|asc)\b)?",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+?)[ \t]*$")
#: A bullet that is a NAME: a filename with an extension, or a directory.
_NAME_RE = re.compile(r"\A[\w.\-]+\.[A-Za-z][\w]{0,7}\Z|\A[\w.\-]+/\Z")

_TABULAR_SUFFIXES = (".tsv", ".csv", ".txt")
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt")


def _scrub_urls(text: str) -> str:
    """Blank out URLs, keeping offsets stable so every index still lines up."""
    return _URL_RE.sub(lambda m: " " * len(m.group(0)), text)


def _evidence(text: str, index: int) -> str:
    """The paragraph a requirement was stated in, collapsed to one line.

    Carried on the item so a nag can quote the spec back. An agent told "your
    header is wrong" argues; an agent shown the sentence it is wrong against
    fixes it.
    """
    if index < 0:
        return ""
    start = text.rfind("\n\n", 0, index) + 2
    end = text.find("\n\n", index)
    if end == -1:
        end = len(text)
    return " ".join(text[start:end].split())[:300]


def _fences(text: str) -> list[tuple[int, str, str]]:
    """Every fenced block as ``(start, language, body)``, in document order."""
    return [(m.start(), m.group(1).lower(), m.group(2)) for m in _FENCE_RE.finditer(text)]


def _fence_after(fences: list[tuple[int, str, str]], index: int, within: int = 600) -> str | None:
    """The body of the first fence that opens just after ``index``.

    ``within`` keeps a requirement from claiming a code block three sections
    later. A spec states the shape next to the sentence that demanded it.
    """
    for start, _lang, body in fences:
        if index <= start <= index + within:
            return body
    return None


def _nearest_path(
    scrubbed: str, index: int, suffixes: tuple[str, ...], dirs: list[tuple[int, str]]
) -> str | None:
    """The last path before ``index`` with one of ``suffixes``.

    A spec names the file, then describes its shape — so the owner of a shape
    requirement is the nearest path above it, not the first in the document.
    A bare filename (``- `manifest.json``` under a heading) is joined to the
    most recent output directory: handed the bare name, a checker would look
    in the workspace root and pass on a file the task never asked for.
    """
    found: str | None = None
    for match in _ANY_PATH_RE.finditer(scrubbed, 0, index):
        candidate = match.group(1)
        if candidate.lower().endswith(suffixes):
            found = candidate
    if found is None or "/" in found:
        return found
    base = ""
    for offset, directory in dirs:
        if offset < index:
            base = directory
    return f"{base}{found}" if base else found


def _split_header_line(line: str) -> tuple[str, ...]:
    """Columns from a header line as a SPEC writes it.

    A markdown spec cannot show a tab, so it renders a tab-delimited header as
    runs of spaces — and a column name may itself contain one space (``Author
    links``). Splitting on single spaces turns six columns into eight and the
    contract becomes unsatisfiable by any correct file.
    """
    line = line.lstrip("﻿").rstrip("\r\n").strip()
    if "\t" in line:
        parts = line.split("\t")
    elif re.search(r"\s{2,}", line):
        parts = re.split(r"\s{2,}", line)
    elif "," in line:
        parts = line.split(",")
    else:
        return ()
    columns = tuple(p.strip().strip("`") for p in parts if p.strip())
    return columns if len(columns) >= 2 else ()


def _delimiter_for(path: str, header_line: str) -> str:
    if "\t" in header_line:
        return "\t"
    return "," if path.lower().endswith(".csv") else "\t"


def _has_traversal(path: str) -> bool:
    """A deliverable path never climbs. Task text is untrusted input."""
    return ".." in Path(path).parts


def _bullet_names(text: str, after: int) -> tuple[tuple[str, ...], bool]:
    """The bullet list that follows ``after``: its names, and whether it is whole.

    Returns ``(names, enumerable)``. ``enumerable`` is False as soon as one
    bullet is prose, because a set with an unknown member cannot say what is
    extra — and a run failed for a file the spec allowed is how a control gets
    switched off.
    """
    names: list[str] = []
    enumerable = True
    seen_any = False
    for line in text[after:].splitlines():
        bullet = _BULLET_RE.match(line)
        if bullet is None:
            if line.strip() == "" and not seen_any:
                continue
            break
        seen_any = True
        entry = bullet.group(1).strip().strip("`").strip()
        if _NAME_RE.match(entry):
            names.append(entry)
        else:
            enumerable = False
    return tuple(names), enumerable


def extract_contract(task_text: str | None) -> DeliverableContract:
    """Everything the task stated explicitly about its output's shape.

    Returns an empty contract for most tasks, and that silence is the design:
    this control speaks only where the task was unambiguous. Every extractor
    needs an explicit anchor phrase; none infers a requirement from an example,
    a mention or a read instruction.
    """
    if not task_text:
        return DeliverableContract()
    text = str(task_text)
    scrubbed = _scrub_urls(text)
    fences = _fences(scrubbed)
    items: list[ContractItem] = []

    # (a) Paths — the contract this module shipped with, unchanged.
    items.extend(
        PathItem(path=path, evidence=_evidence(text, scrubbed.find(path)))
        for path in required_deliverables(text)
        if not _has_traversal(path)
    )

    # (b) Exact output sets. Recorded first: the directory they name is what a
    # bare filename further down the spec belongs to.
    dirs: list[tuple[int, str]] = []
    for match in _OUTPUT_SET_RE.finditer(scrubbed):
        directory = match.group(2) or match.group(3) or ""
        if not directory or _has_traversal(directory):
            continue
        dirs.append((match.start(), directory))
        names, enumerable = _bullet_names(scrubbed, match.end())
        if not names:
            continue
        said_exactly = bool(match.group(1) or match.group(4))
        tail = scrubbed[match.end() : match.end() + 800]
        forbid = (said_exactly or bool(_NO_OTHERS_RE.search(tail))) and enumerable
        items.append(
            ExactSetItem(
                directory=directory,
                names=names,
                forbid_extra=forbid,
                evidence=_evidence(text, match.start()),
            )
        )

    # (c) Headers, from a fenced header line or an explicit columns list.
    headers: dict[str, HeaderItem] = {}
    for match in _HEADER_ANCHOR_RE.finditer(scrubbed):
        body = _fence_after(fences, match.end())
        if not body:
            continue
        line = next((raw for raw in body.splitlines() if raw.strip()), "")
        columns = _split_header_line(line)
        path = _nearest_path(scrubbed, match.start(), _TABULAR_SUFFIXES, dirs)
        if not columns or not path or _has_traversal(path):
            continue
        headers[path] = HeaderItem(
            path=path,
            columns=columns,
            delimiter=_delimiter_for(path, line),
            raw=line,
            evidence=_evidence(text, match.start()),
        )
    for match in _COLUMNS_RE.finditer(scrubbed):
        path = _nearest_path(scrubbed, match.start(), _TABULAR_SUFFIXES, dirs)
        if not path or path in headers or _has_traversal(path):
            continue
        columns = tuple(p.strip().strip("`") for p in match.group(1).split(",") if p.strip())
        if len(columns) < 2:
            continue
        headers[path] = HeaderItem(
            path=path,
            columns=columns,
            delimiter="," if path.lower().endswith(".csv") else "\t",
            evidence=_evidence(text, match.start()),
        )
    items.extend(headers.values())

    # (d) JSON field sets, from the fenced example beside the requirement.
    for match in _JSON_FIELDS_ANCHOR_RE.finditer(scrubbed):
        body = _fence_after(fences, match.end())
        path = _nearest_path(scrubbed, match.start(), (".json",), dirs)
        if not body or not path or _has_traversal(path):
            continue
        try:
            example = json.loads(body)
        except ValueError:
            continue
        first: dict[str, object] | None
        if isinstance(example, list):
            container = "array"
            first = next((e for e in example if isinstance(e, dict)), None)
        elif isinstance(example, dict):
            container, first = "object", example
        else:
            continue
        if not first:
            continue
        items.append(
            JsonFieldsItem(
                path=path,
                fields=tuple(first.keys()),
                container=container,
                evidence=_evidence(text, match.start()),
            )
        )

    # (e) Section headings, from the fenced structure the spec forbade renaming.
    seen_sections: set[tuple[str, tuple[str, ...]]] = set()
    for match in _SECTIONS_ANCHOR_RE.finditer(scrubbed):
        body = _fence_after(fences, match.end())
        path = _nearest_path(scrubbed, match.end(), _DOC_SUFFIXES, dirs)
        if not body or not path or _has_traversal(path):
            continue
        headings = tuple(h.group(2).strip() for h in _HEADING_RE.finditer(body))
        if not headings or (path, headings) in seen_sections:
            continue
        seen_sections.add((path, headings))
        items.append(
            SectionsItem(path=path, headings=headings, evidence=_evidence(text, match.start()))
        )

    # (f) Sort order — only where a header says what the key names mean.
    for match in _SORT_RE.finditer(scrubbed):
        path = _nearest_path(scrubbed, match.start(), _TABULAR_SUFFIXES, dirs)
        if not path or path not in headers:
            continue
        keys = tuple(k.strip() for k in match.groups() if k and k.strip())
        if not keys or any(k not in headers[path].columns for k in keys):
            continue
        items.append(SortItem(path=path, keys=keys, evidence=_evidence(text, match.start())))

    return DeliverableContract(items=tuple(items))


# ─── Checking ─────────────────────────────────────────────────────────


def _resolve_under(root: Path, root_resolved: Path, spec_path: str) -> Path | None:
    """Where in THIS workspace the spec's path lives, or None if nowhere legal.

    A task states an absolute path inside the sandbox it was written for
    (``/tmp_workspace/results/x.tsv``). The run's workspace is that directory
    on the box that executes it, and in a test it is a temporary directory —
    so the literal string is tried first and, when it lands outside the
    workspace, the sandbox root is stripped and the remainder joined to the
    real one.

    Everything returned is confined to ``root``. Task text is untrusted in any
    deployment where someone else can file a task, and these paths reach the
    filesystem: the path that is finally touched is always rebuilt from the
    trusted root, never used as the task wrote it.
    """
    raw = Path(spec_path)
    candidates: list[Path] = []
    if raw.is_absolute():
        parts = raw.parts[1:]
        candidates.append(raw)
        if len(parts) > 1:
            candidates.append(root.joinpath(*parts[1:]))
        candidates.append(root.joinpath(*parts))
    else:
        candidates.append(root / raw)

    fallback: Path | None = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root_resolved)
        except (ValueError, OSError):
            continue
        if resolved.exists():
            return resolved
        if fallback is None:
            fallback = resolved
    return fallback


def _display(root_resolved: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root_resolved))
    except ValueError:
        return str(path)


def _listed(names: list[str] | tuple[str, ...]) -> str:
    shown = [f"`{n}`" for n in list(names)[:_MAX_LISTED]]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")


def _read_text(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_MAX_READ_BYTES)
    except OSError:
        return None


def _first_line(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readline(64_000)
    except OSError:
        return None


def _normalise_heading(text: str) -> str:
    """How a heading is compared: by its NAME, not by its markdown.

    The spec said do not rename these headings, not do not re-nest them.
    Failing a run over a ``###`` where the fence showed ``####``, or over a
    trailing colon, would be a verdict about markdown rather than about the
    deliverable — and it is the false-positive class that gets a control muted.
    """
    return " ".join(text.replace("#", " ").split()).rstrip(":").strip().casefold()


def _neighbours(root_resolved: Path, target: Path, suffix: str) -> list[str]:
    """Files of the same kind sitting next to where the deliverable should be.

    "Not found" alone sends an agent looking for work it has already done: on
    the measured run the manifest was written to the workspace root under
    another name. Naming what IS there turns the verdict into a move.
    """
    found: list[str] = []
    for directory in (target.parent, root_resolved):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix.lower() != suffix.lower() or entry == target:
                continue
            name = _display(root_resolved, entry)
            if name not in found:
                found.append(name)
            if len(found) >= 2:
                return found
    return found


def _check_path(item: PathItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    target = _resolve_under(root, root_resolved, item.path)
    if target is None:
        return None
    shown = _display(root_resolved, target)
    try:
        ok = target.is_file() and target.stat().st_size > 0
    except OSError:
        ok = False
    if ok:
        return ItemFinding(item, STATUS_OK)
    return ItemFinding(
        item,
        STATUS_MISSING,
        f"`{shown}` does not exist, or is empty — the task named it as an output.",
    )


def _check_exact_set(item: ExactSetItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    target = _resolve_under(root, root_resolved, item.directory)
    if target is None:
        return None
    shown = _display(root_resolved, target)
    if not target.is_dir():
        return ItemFinding(
            item,
            STATUS_MISSING,
            f"`{shown}/` does not exist; the task said to create {_listed(item.names)} in it.",
        )
    missing: list[str] = []
    for name in item.names:
        child = target / name.rstrip("/")
        if name.endswith("/"):
            if not child.is_dir():
                missing.append(name)
        else:
            try:
                if not (child.is_file() and child.stat().st_size > 0):
                    missing.append(name)
            except OSError:
                missing.append(name)
    extras: list[str] = []
    if item.forbid_extra:
        allowed = {n.rstrip("/") for n in item.names}
        try:
            # Dotfiles are the workspace's own bookkeeping (`.gitkeep`, tool
            # state), never something the agent chose to produce.
            extras = sorted(
                e.name
                for e in target.iterdir()
                if e.name not in allowed and not e.name.startswith(".")
            )
        except OSError:
            extras = []
    if not missing and not extras:
        return ItemFinding(item, STATUS_OK)
    reasons = []
    if missing:
        reasons.append(f"`{shown}/` is missing {_listed(missing)}")
    if extras:
        reasons.append(
            f"`{shown}/` contains {_listed(extras)}, which the task did not ask for "
            f"(it said to create exactly {_listed(item.names)})"
        )
    return ItemFinding(
        item,
        STATUS_MISSING if missing else STATUS_MISMATCH,
        "; ".join(reasons) + ".",
    )


def _check_header(item: HeaderItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    target = _resolve_under(root, root_resolved, item.path)
    if target is None:
        return None
    shown = _display(root_resolved, target)
    required = item.required_text
    line = _first_line(target) if target.is_file() else None
    if line is None:
        return ItemFinding(
            item,
            STATUS_MISSING,
            f"`{shown}` does not exist, so it cannot carry the header the task required "
            f"(`{required}`).",
        )
    # A BOM, a CRLF or a padded cell is how the file travelled, not what the
    # agent chose to call its columns. Only the NAMES are the contract.
    cells = [c.strip() for c in line.lstrip("﻿").rstrip("\r\n").rstrip().split(item.delimiter)]
    while cells and cells[-1] == "":
        cells.pop()
    if len(cells) < 2 <= len(item.columns):
        shown_delimiter = "a tab" if item.delimiter == "\t" else f"`{item.delimiter}`"
        return ItemFinding(
            item,
            STATUS_MISMATCH,
            f"`{shown}` does not use {shown_delimiter} between its columns; the task requires "
            f"the header `{required}`.",
        )
    actual = " ".join(" ".join(cells).split())
    if actual == required:
        return ItemFinding(item, STATUS_OK)
    # Named per WORD, not per column: the split of an ambiguously-spaced spec
    # fence may have glued two columns together, and reporting `Authors
    # Abstract` as missing from a file that has both is a fault in the reader,
    # not in the file. A column counts as dropped only when a word of it is
    # nowhere in the header the agent wrote.
    present = set(actual.split())
    dropped = [c for c in item.columns if not set(c.split()) <= present]
    reason = f"`{shown}` header is `{actual}`; the task requires `{required}`"
    if dropped:
        reason += f" — missing required columns {_listed(dropped)}"
    return ItemFinding(item, STATUS_MISMATCH, reason + ".")


def _check_json_fields(item: JsonFieldsItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    target = _resolve_under(root, root_resolved, item.path)
    if target is None:
        return None
    shown = _display(root_resolved, target)
    if not target.is_file():
        reason = f"`{shown}` not found"
        nearby = _neighbours(root_resolved, target, ".json")
        if nearby:
            reason += f"; the workspace has {_listed(nearby)} instead"
        return ItemFinding(item, STATUS_MISSING, reason + ".")
    raw = _read_text(target)
    try:
        data = json.loads(raw or "")
    except ValueError:
        return ItemFinding(item, STATUS_MISMATCH, f"`{shown}` is not valid JSON.")
    if item.container == "array" and isinstance(data, dict):
        return ItemFinding(
            item,
            STATUS_MISMATCH,
            f"`{shown}` is a JSON object; the task requires a JSON array whose items carry "
            f"{_listed(item.fields)}.",
        )
    if item.container == "array":
        if not isinstance(data, list) or not data:
            return ItemFinding(
                item, STATUS_MISMATCH, f"`{shown}` contains no items to check against the contract."
            )
        first = next((e for e in data if isinstance(e, dict)), None)
        if first is None:
            return ItemFinding(
                item, STATUS_MISMATCH, f"`{shown}` is a JSON array of values, not of objects."
            )
    elif isinstance(data, dict):
        first = data
    else:
        return ItemFinding(item, STATUS_MISMATCH, f"`{shown}` is not a JSON object.")
    present = list(first.keys())
    missing = [f for f in item.fields if f not in present]
    unexpected = [f for f in present if f not in item.fields]
    if not missing and not unexpected:
        return ItemFinding(item, STATUS_OK)
    parts = []
    if missing:
        parts.append(f"missing {_listed(missing)}")
    if unexpected:
        parts.append(f"carries {_listed(unexpected)}, which the task did not list")
    return ItemFinding(
        item,
        STATUS_MISMATCH,
        f"`{shown}` items must contain exactly {_listed(item.fields)}: " + "; ".join(parts) + ".",
    )


def _check_sections(item: SectionsItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    target = _resolve_under(root, root_resolved, item.path)
    if target is None:
        return None
    shown = _display(root_resolved, target)
    text = _read_text(target) if target.is_file() else None
    if text is None:
        return ItemFinding(item, STATUS_MISSING, f"`{shown}` does not exist.")
    present = {_normalise_heading(m.group(2)) for m in _HEADING_RE.finditer(text)}
    missing = [h for h in item.headings if _normalise_heading(h) not in present]
    if not missing:
        return ItemFinding(item, STATUS_OK)
    return ItemFinding(
        item,
        STATUS_MISMATCH,
        f"`{shown}` is missing the section headings the task said not to rename: "
        f"{_listed(missing)}.",
    )


def _check_sort(
    item: SortItem, header: HeaderItem | None, root: Path, root_resolved: Path
) -> ItemFinding | None:
    """Sortedness, but only on a file whose header we already agree about.

    When the header is wrong the header item already carries that fault, and
    reporting `Track` as an unknown column on top of it is the same finding
    twice — which is how a report stops being read.
    """
    if header is None:
        return None
    target = _resolve_under(root, root_resolved, item.path)
    if target is None or not target.is_file():
        return None
    text = _read_text(target)
    if not text:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    actual = tuple(c.strip() for c in lines[0].lstrip("﻿").rstrip().split(header.delimiter))
    if actual != header.columns:
        return None
    try:
        indices = [header.columns.index(key) for key in item.keys]
    except ValueError:
        return None
    previous: tuple[str, ...] | None = None
    for number, line in enumerate(lines[1:], start=1):
        cells = line.split(header.delimiter)
        if max(indices) >= len(cells):
            continue
        current = tuple(cells[i].strip() for i in indices)
        if previous is not None and current < previous:
            shown = _display(root_resolved, target)
            return ItemFinding(
                item,
                STATUS_MISMATCH,
                f"`{shown}` is not sorted by {_listed(item.keys)} ascending: row {number + 1} "
                f"({_listed(current)}) comes after row {number} ({_listed(previous)}).",
            )
        previous = current
    return ItemFinding(item, STATUS_OK)


def check_contract(contract: DeliverableContract, root: str | Path) -> ContractReport:
    """Read the workspace against the contract, item by item.

    Pure and importable: no database, no session, no flag. Every path is
    confined to ``root`` before it is touched, and an item whose path lands
    outside it produces no finding at all — a file the agent could not have
    been graded on is not this contract's business.
    """
    try:
        root_path = Path(root)
        root_resolved = root_path.resolve()
    except (OSError, ValueError):
        return ContractReport()
    headers = {i.path: i for i in contract.items if isinstance(i, HeaderItem)}
    findings: list[ItemFinding] = []
    for item in contract.items:
        finding: ItemFinding | None
        if isinstance(item, PathItem):
            finding = _check_path(item, root_path, root_resolved)
        elif isinstance(item, ExactSetItem):
            finding = _check_exact_set(item, root_path, root_resolved)
        elif isinstance(item, HeaderItem):
            finding = _check_header(item, root_path, root_resolved)
        elif isinstance(item, JsonFieldsItem):
            finding = _check_json_fields(item, root_path, root_resolved)
        elif isinstance(item, SectionsItem):
            finding = _check_sections(item, root_path, root_resolved)
        elif isinstance(item, SortItem):
            finding = _check_sort(item, headers.get(item.path), root_path, root_resolved)
        else:  # pragma: no cover — the union is closed
            finding = None
        if finding is not None:
            findings.append(finding)
    return ContractReport(findings=tuple(findings))
