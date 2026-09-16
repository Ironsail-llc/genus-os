"""Reading the workspace against the contract.

The filesystem half: resolve each item's path inside the run's workspace, read
what is there, and say in one line how it differs from what the task asked for.
Pure in the same sense as its counterpart — no flag, no session, no database —
and confined: every path is rebuilt from the trusted root before it is touched.

Split out of ``deliverable_contract`` 2026-09-16 (hostile review I6). Its
counterpart is ``deliverable_extract``; the two share only
``deliverable_items``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.engine.deliverable_items import (
    _MAX_JSON_BYTES,
    _MAX_READ_BYTES,
    _MAX_SCANNED_NEIGHBOURS,
    _MAX_STREAM_LINES,
    STATUS_MISMATCH,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_UNCHECKED,
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

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Headings are compared by name; the pattern lives here because only the
#: checker reads a written document.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

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


def _read_text(path: Path, limit: int | None = None) -> str | None:
    """The whole file, or None when it is larger than ``limit``.

    Never a PREFIX. It used to return the first 2,000,000 characters and every
    caller treated them as the file, so a valid 2.5 MB JSON manifest read as
    "not valid JSON" and a correctly-sorted 18 MB TSV read as "not sorted",
    with a precise row number naming a row that did not exist as described —
    the 2 MB cut had halved a line and the fragment sorted below its
    predecessor (hostile review 2026-09-16, C4). Under `enforce` each of those
    failed an otherwise-correct run.

    "Too large to verify" is a truthful silence; a mismatch computed on a
    prefix is a lie, and it is the more expensive of the two.
    """
    limit = _MAX_READ_BYTES if limit is None else limit
    try:
        if path.stat().st_size > limit:
            return None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit + 1) or ""
    except OSError:
        return None


class _TooManyRowsError(Exception):
    """More rows than the sort check will read. A silence, not a verdict."""


def _iter_lines(path: Path, limit: int | None = None) -> Iterator[str]:
    """Non-empty lines, one at a time, never more than ``limit`` of them.

    The sort check reads rows, not a document, so it streams rather than
    slicing — an 18 MB TSV is ordinary for these tasks and its size says
    nothing about whether its rows ascend. Materialising the lines instead
    cost 107 MB of resident memory on that file, and the check only ever
    compares a row with the one before it.

    Raises past the cap rather than returning what it has, because a truncated
    read is exactly what produced the verdict this replaced.
    """
    limit = _MAX_STREAM_LINES if limit is None else limit
    seen = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            seen += 1
            if seen > limit:
                raise _TooManyRowsError
            yield raw


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
        # `iterdir` is lazy, so the error for an absent directory arrives on
        # the first step of the loop, not on the call — and the absent
        # directory is exactly the case this hint is for.
        #
        # Streamed and capped, not sorted: a workspace can hold tens of
        # thousands of downloaded inputs, and this is a hint attached to a
        # verdict, not a search.
        try:
            for scanned, entry in enumerate(directory.iterdir()):
                if scanned >= _MAX_SCANNED_NEIGHBOURS:
                    break
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                if entry.suffix.lower() != suffix.lower() or entry == target:
                    continue
                name = _display(root_resolved, entry)
                if name not in found:
                    found.append(name)
                if len(found) >= 2:
                    return found
        except OSError:
            continue
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


#: Most matches a pattern check will look at. A glob over a workspace with
#: tens of thousands of downloaded inputs is a search, and this is a check.
_MAX_PATTERN_MATCHES = 5_000


def _check_pattern(item: PatternItem, root: Path, root_resolved: Path) -> ItemFinding | None:
    """At least one real file matching the shape the task described.

    Deliberately weak, and that is the point: the spec used a placeholder, so
    the only thing it actually promised is that files of this shape exist in
    this place. Asking for more would be inventing a requirement; asking for
    less — the literal placeholder path — failed a correct run at `enforce`
    (re-review 2026-09-16, R1).

    Empty matches do not count, by the same rule the rest of this module holds:
    a touched path is not a produced deliverable.
    """
    try:
        for seen, match in enumerate(root.glob(item.pattern)):
            if seen >= _MAX_PATTERN_MATCHES:
                break
            resolved = match.resolve()
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                continue
            if resolved.is_file() and resolved.stat().st_size > 0:
                return ItemFinding(item, STATUS_OK)
    except (OSError, ValueError, IndexError):
        return None
    return ItemFinding(
        item,
        STATUS_MISSING,
        f"nothing matches `{item.pattern}` — the task described its outputs with a "
        "placeholder, so the names are yours to choose but the directory and the "
        "extension are not.",
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
    if not cells:
        # The commonest failure of all, and it used to be reported as "does not
        # use a tab between its columns" — true of an empty file, and a remedy
        # pointing at the wrong problem.
        return ItemFinding(
            item,
            STATUS_MISSING,
            f"`{shown}` is empty; the task requires the header `{required}` and its rows.",
        )
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
    raw = _read_text(target, _MAX_JSON_BYTES)
    if raw is None:
        # Parsed whole or not at all: a 2.5 MB manifest read to 2 MB and
        # handed to `json.loads` reported "not valid JSON" about a valid file.
        return ItemFinding(
            item, STATUS_UNCHECKED, f"`{shown}` is too large to verify; not checked."
        )
    try:
        data = json.loads(raw)
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
    if not target.is_file():
        return ItemFinding(item, STATUS_MISSING, f"`{shown}` does not exist.")
    text = _read_text(target)
    if text is None:
        # Headings past the cap would have read as missing.
        return ItemFinding(
            item, STATUS_UNCHECKED, f"`{shown}` is too large to verify; not checked."
        )
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
    try:
        indices = [header.columns.index(key) for key in item.keys]
    except ValueError:
        return None
    previous: tuple[str, ...] | None = None
    number = 0
    try:
        for row, line in enumerate(_iter_lines(target)):
            if row == 0:
                actual = tuple(
                    c.strip() for c in line.lstrip("\ufeff").rstrip().split(header.delimiter)
                )
                if actual != header.columns:
                    return None
                continue
            number = row
            cells = line.rstrip("\r\n").split(header.delimiter)
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
    except (_TooManyRowsError, OSError):
        # A silence, not a verdict. The 2 MB slice this replaced halved a line,
        # and the fragment sorted below its predecessor: a correct 18 MB file
        # was reported unsorted at a row number that did not exist as described.
        return None
    if number == 0:
        return None
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
        elif isinstance(item, PatternItem):
            finding = _check_pattern(item, root_path, root_resolved)
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
