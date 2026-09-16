"""What a deliverable contract is made of.

The shared vocabulary of the three modules that were one: the typed items an
extractor produces, the statuses a checker returns, and the report that carries
them. Nothing here reads a file, a flag or a database — it is the only part
both halves may import, which is what keeps the seam a seam.

Split out of ``deliverable_contract`` 2026-09-16 after hostile review: that file
had reached 1,567 lines and its own section comments already drew the line
between extraction and checking, which share nothing but these definitions.
"""

from __future__ import annotations

from dataclasses import dataclass

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

#: A JSON manifest is parsed WHOLE or not at all. Larger than this and the
#: check reports nothing rather than a verdict computed on a fragment.
_MAX_JSON_BYTES = 64_000_000

#: Rows the sort check will stream before it declines to answer. A file with
#: more rows than this is not a deliverable a reader checks by eye either.
_MAX_STREAM_LINES = 2_000_000

#: Most names a single reason will list before it summarises the rest. A wall
#: of names is the same defect as no names at all.
_MAX_LISTED = 10

#: How far a "what is there instead" hint will look. A benchmark workspace
#: holds thousands of downloaded inputs and this is a hint, not a search.
_MAX_SCANNED_NEIGHBOURS = 2_000


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


@dataclass(frozen=True)
class PatternItem:
    """A family of files the task described with a placeholder.

    Re-review 2026-09-16 (R1). A spec that says "for each page, create
    `results/scp-XXX/`" is not naming a file — `XXX` is its own stand-in for the
    page number, and a correct run writes `scp-173/`, `scp-096/`, never
    `scp-XXX/`. Read as a literal path it is unsatisfiable by construction, and
    at `enforce` it failed a correct run.

    The remedy is not silence: the sentence still states a real contract — this
    directory, this extension, one file per thing — and `PatternItem` checks
    exactly that much. `pattern` is workspace-relative with the template
    segment replaced by `*`, so the literal parts the spec did mean (`scp-`,
    `text.md`) still have to hold.
    """

    pattern: str
    evidence: str = ""


ContractItem = (
    PathItem | PatternItem | ExactSetItem | HeaderItem | JsonFieldsItem | SectionsItem | SortItem
)


@dataclass(frozen=True)
class DeliverableContract:
    """Everything one task said about the shape of its output."""

    items: tuple[ContractItem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)


STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_MISMATCH = "mismatch"
#: The file is there and this check could not read it whole — too large to
#: verify. NOT a failure: "I did not check" and "it is wrong" are different
#: sentences, and reporting the second when the first is true failed correct
#: runs (hostile review 2026-09-16, C4). Counted with `ok` for the verdict and
#: named in the report so the silence is visible rather than merely quiet.
STATUS_UNCHECKED = "unchecked"


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
        return tuple(f for f in self.findings if f.status not in (STATUS_OK, STATUS_UNCHECKED))

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


#: Most names a single reason will list before it summarises the rest. A wall
#: of names is the same defect as no names at all.


def listed(names: list[str] | tuple[str, ...]) -> str:
    """Names in a reason, capped. Shared by the checker and the sticky block."""
    shown = [f"`{n}`" for n in list(names)[:_MAX_LISTED]]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")
