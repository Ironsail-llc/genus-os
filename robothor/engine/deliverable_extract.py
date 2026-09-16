"""Reading a contract out of what the task said.

The regex half of the deliverable contract: output verbs, prohibition
suppressors, fenced headers, JSON field sets, section headings and sort keys,
in English and in Chinese. Pure — it takes text and returns items, touches no
filesystem, and is safe to run on untrusted task text.

Split out of ``deliverable_contract`` 2026-09-16 (hostile review I6). Its
counterpart is ``deliverable_check``; the two share only
``deliverable_items``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from robothor.engine.deliverable_items import (
    ContractItem,
    DeliverableContract,
    ExactSetItem,
    HeaderItem,
    JsonFieldsItem,
    PathItem,
    SectionsItem,
    SortItem,
)

#: Verbs that introduce an OUTPUT. "read from", "load", "open" deliberately
#: absent: naming an input file is not promising to create it.
_OUTPUT_VERB = r"(?:save|write|store|export|output|put|place|dump|emit)"

#: A concrete local path: at least one path-ish character and a real
#: extension. Bare extensions (".tsv") and URLs are excluded by construction
#: -- the negative lookbehind keeps us off "://host/results/x.tsv".
#:
#: Deliberately ASCII, not ``\w``. Python's ``\w`` matches CJK, so
#: "result.json格式规范" came back as one path named `result.json格式规范` and
#: "结果result.json" as `结果result.json` — the sibling module
#: ``deliverables.py`` already carries this exact lesson in its own docstring
#: ("Deliberately ASCII, not `\w`: Python's `\w` matches CJK, which would glue
#: a path to the sentence around it") and it was not carried across (hostile
#: review 2026-09-16, I2).
_PATH = (
    r"(?<![A-Za-z0-9_:/.])"
    r"((?:/|\.{1,2}/)?(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.[A-Za-z][A-Za-z0-9]{0,7})"
)

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

#: The same contract, stated in Chinese. 37 of the 60 benchmark specs write
#: their output requirements in CJK and the extractor was blind to all of them
#: — a silent skip rather than a false positive, so safe, but it meant the
#: "60 specs, 0 wrong" sweep was really "23 specs" (hostile review 2026-09-16,
#: I2).
#:
#: Deliberately minimal, and held to the same conservatism as the English
#: anchors: an explicit output VERB (保存 save / 写入 write / 输出 output /
#: 存储 store) pointed at a concrete path, or the "output file path:" label
#: these specs use as a heading. Chinese has no space-delimited words, so
#: there is no `\b` to lean on — the anchor sits immediately against the path
#: gap instead, which is how these specs are actually written.
_ZH_OUTPUT_VERB = "(?:保存|写入|输出|存储|存入|导出)"

#: "输出文件路径：/tmp_workspace/results/result.json" — a labelled declaration
#: rather than a sentence, and the commonest shape in this corpus.
_ZH_OUTPUT_LABEL = "(?:输出文件路径|输出路径|结果文件|输出文件)"

#: What may sit between a Chinese anchor and the path: a full-width or ASCII
#: colon, whitespace, a bullet, a backtick. No preposition — Chinese puts the
#: target after the verb directly, or after 到/至.
_ZH_GAP = r"(?:到|至)?[ \t]*[：:]?[ \t]*(?:\r?\n[ \t]*)*(?:[-*+][ \t]+)?`?"

_ZH_CONTRACT_PATTERNS = [
    re.compile(rf"{_ZH_OUTPUT_VERB}{_ZH_GAP}{_PATH}"),
    re.compile(rf"{_ZH_OUTPUT_LABEL}{_ZH_GAP}{_PATH}"),
]

#: Chinese prohibition and hypothetical markers, the counterpart of
#: ``_SUPPRESSOR_RE``. Without these the zh anchors would reintroduce exactly
#: the C3a defect in a second language.
_ZH_SUPPRESSOR_RE = re.compile(
    "(?:不要|不得|禁止|请勿|切勿|不能|无需|不需要|如果|若|假如|例如|比如|示例|之前.{0,8}要求)"
)

#: Anything inside one of these is a URL, not a local deliverable.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)

#: Words that turn "save it to P" into something other than a requirement to
#: save it to P: a prohibition, a hypothetical, a report of what someone else
#: was told, or an illustration.
#:
#: MEASURED by hostile review 2026-09-16 (C3a): five of five prohibition
#: phrasings produced a `PathItem`, so at `enforce` the engine re-asked the
#: agent to write the file the task had forbidden and then failed the run when
#: it refused. Two Safety specs name a path for content an agent should
#: decline. The asymmetry decides the design: suppressing a real deliverable
#: costs a false negative, which is this module's declared safe failure;
#: demanding a forbidden one costs a run, an operator alert, and an agent
#: pushed toward producing the thing it was told not to produce.
_SUPPRESSOR_RE = re.compile(
    r"\b(?:"
    r"do\s+not|don'?t|does\s+not|doesn'?t|never|no\s+circumstances|"
    r"must\s+not|should\s+not|shouldn'?t|cannot|can'?t|won'?t|will\s+not|"
    r"avoid|refrain\s+from|refuse[sd]?|declines?|"
    r"if|unless|when(?:ever)?\s+asked|instead\s+of|rather\s+than|"
    r"was\s+told\s+to|were\s+told\s+to|had\s+been\s+asked|previously\s+asked|"
    # Hypothetical modals only. `can` and `may` are left out on purpose: "you
    # can save it to X" is how half of real instructions are phrased, and
    # suppressing those would disarm the control on ordinary tasks.
    r"could|might|would\s+(?:be|go)|"
    r"for\s+example|for\s+instance|such\s+as|some\s+teams"
    r")\b",
    re.IGNORECASE,
)

#: Where a suppressor stops applying. A clause boundary, because "Do not
#: include markdown. Save the table to results/x.tsv" is a requirement and
#: "Do not save the table to results/x.tsv" is not, and the only thing
#: separating them is the full stop.
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?]\s|\n|[。；！？]")

#: "e.g." / "i.e." — an illustration whose own full stop would otherwise be
#: read as the end of the clause that introduced it.
_ABBREVIATION_RE = re.compile(r"\b[ei]\.?\s?[ge]\.", re.IGNORECASE)

#: How close the abbreviation has to sit to count. It introduces the very next
#: clause, not the paragraph.
_ABBREVIATION_REACH = 40

#: How far back a suppressor can reach inside its own clause. Long enough for
#: "Under no circumstances should you write the credentials to …", short
#: enough that a prohibition two sentences up cannot disarm a real requirement.
_SUPPRESSOR_REACH = 160


def _is_suppressed(text: str, index: int) -> bool:
    """Does a prohibition, hypothetical or illustration govern this match?

    Looks back to the nearest clause boundary — never across one — within a
    bounded window.
    """
    whole = text[max(0, index - _SUPPRESSOR_REACH) : index]
    # `e.g.` and `i.e.` carry a full stop that is not a clause break, so they
    # have to be read BEFORE the window is trimmed — trimming at their own
    # period is what let "e.g. save the output to results/x.json" through.
    if _ABBREVIATION_RE.search(whole[-_ABBREVIATION_REACH:]):
        return True
    window = whole
    breaks = list(_CLAUSE_BREAK_RE.finditer(window))
    if breaks:
        window = window[breaks[-1].end() :]
    if _SUPPRESSOR_RE.search(window) is not None:
        return True
    return _ZH_SUPPRESSOR_RE.search(window) is not None


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
    for pattern in (*_CONTRACT_PATTERNS, *_ZH_CONTRACT_PATTERNS):
        for match in pattern.finditer(scrubbed):
            path = match.group(1)
            # The verb, not the path: "Do not save X to P" puts the negation
            # before the verb and the path at the end of a long clause.
            if _is_suppressed(scrubbed, match.start()):
                continue
            if path not in found:
                found.append(path)
    return found


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

#: "Save a JSON array" / "a JSON object" — the container named in words.
_STATED_CONTAINER_RE = re.compile(r"\bJSON\s+(array|object)\b", re.IGNORECASE)

#: How far back to look for it. The sentence that introduces the fence is
#: normally the one immediately before the requirement.
_CONTAINER_REACH = 300


def _stated_container(text: str, index: int) -> str | None:
    """The container the spec named in prose, or None if it named none."""
    window = text[max(0, index - _CONTAINER_REACH) : index]
    found = None
    for match in _STATED_CONTAINER_RE.finditer(window):
        found = match.group(1).lower()
    return found


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
                # The PROSE wins where it is explicit. A spec that says "Save a
                # JSON array" and then draws one object in the fence is the
                # brief's named adversarial case, and inferring the container
                # from the drawing failed a correct array file for being "not a
                # JSON object" (hostile review 2026-09-16, I1). The example is
                # an illustration of an item; the sentence is the requirement.
                container=_stated_container(scrubbed, match.start()) or container,
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
