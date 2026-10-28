"""Text out of the document formats an operator actually sends.

One answer for both readers: ``read_file`` (a path the agent was pointed at)
and the Telegram intake (bytes the operator attached). Before this module,
``read_file`` died with a UTF-8 decode error on any binary file and the intake
extracted PDF and nothing else, so a Word document, a spreadsheet or a deck
reached the agent as a path it could not open.

Every parsing library is imported lazily, inside the function that needs it.
An install without one gets :class:`ExtractorUnavailableError` naming the package
to install — never an ImportError at module import, which would take
``read_file`` down for plain text too.

**Extraction has a budget.** Callers pass the number of characters they will
actually show (``max_chars``), and every extractor stops walking the document
— pages, paragraphs, slides, sheets, rows — once that much text is in hand.
The work runs on a worker thread that a tool timeout cannot cancel, so a
1,000-page PDF must not be parsed whole to return its first twenty pages.
Text cut by the budget ends with :data:`STOPPED_EARLY`.
"""

from __future__ import annotations

import io
import mimetypes
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

__all__ = [
    "STOPPED_EARLY",
    "XLSX_MAX_ROWS_PER_SHEET",
    "ExtractionError",
    "ExtractorUnavailableError",
    "binary_summary",
    "describe_bytes",
    "document_kind",
    "extract_bytes",
    "extract_path",
    "looks_binary",
]

#: Rows quoted per worksheet. A spreadsheet with a million rows would
#: otherwise be rendered whole only to be cut at the caller's character cap.
XLSX_MAX_ROWS_PER_SHEET = 2_000

#: Start of the note that ends a text cut by the caller's ``max_chars``.
STOPPED_EARLY = "[... extraction stopped at"

#: How long the ``pdftotext`` fallback may run on one file.
_PDFTOTEXT_TIMEOUT_S = 60

_PDF_NO_TEXT = "[PDF: no extractable text (may be image-based)]"

_SUFFIX_KIND = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".docm": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".pptx": "pptx",
    ".pptm": "pptx",
}

_MIME_KIND = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-word.document.macroenabled.12": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
}

_PACKAGE = {"docx": "python-docx", "xlsx": "openpyxl", "pptx": "python-pptx"}

#: python-docx refuses any main part but a plain document's. A macro-enabled
#: document's body is the same WordprocessingML, so the content type is
#: relabelled in memory before it is opened. The macros are never run.
_DOCM_MAIN = b"application/vnd.ms-word.document.macroEnabled.main+xml"
_DOCX_MAIN = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


class ExtractionError(Exception):
    """The file could not be turned into text (corrupt, encrypted, ...)."""


class ExtractorUnavailableError(ExtractionError):
    """The library this format needs is not installed."""


def document_kind(suffix: str, mime: str = "") -> str | None:
    """``"pdf"``, ``"docx"``, ``"xlsx"``, ``"pptx"`` or None.

    Suffix first — it is the sender's own label — then MIME, for the
    platforms that report a type and a name without an extension.
    """
    kind = _SUFFIX_KIND.get((suffix or "").lower())
    if kind:
        return kind
    return _MIME_KIND.get((mime or "").strip().lower())


def extract_bytes(raw: bytes, kind: str, *, max_chars: int | None = None) -> str:
    """The text of a document of ``kind``. Raises :class:`ExtractionError`.

    With ``max_chars``, reading stops once that many characters are in hand;
    the text is cut there and ends with a :data:`STOPPED_EARLY` note.
    """
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        raise ExtractionError(f"no text extractor for {kind!r} files")
    try:
        return extractor(raw, max_chars)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - every parser has its own error zoo
        raise ExtractionError(f"could not read this {kind} file: {exc}") from exc


def extract_path(path: Path, kind: str, *, max_chars: int | None = None) -> str:
    """:func:`extract_bytes` on a file on disk."""
    return extract_bytes(Path(path).read_bytes(), kind, max_chars=max_chars)


def describe_bytes(raw: bytes, kind: str, *, max_chars: int | None = None) -> str:
    """:func:`extract_bytes` that never raises: a failure becomes a note.

    For the channel layer, where the file is already saved and the worst case
    is an agent told why it got no text.
    """
    try:
        return extract_bytes(raw, kind, max_chars=max_chars)
    except ExtractionError as exc:
        return f"[{kind.upper()} text extraction failed: {exc}]"


def looks_binary(sample: bytes) -> bool:
    """A NUL byte in the first block is the cheap, reliable binary tell."""
    return b"\x00" in sample


def binary_summary(path: Path) -> dict[str, Any]:
    """What can honestly be said about a file there is no reader for."""
    path = Path(path)
    mime, _ = mimetypes.guess_type(path.name)
    size = path.stat().st_size
    return {
        "path": str(path),
        "binary": True,
        "size": size,
        "mime": mime or "application/octet-stream",
        "message": (
            f"Binary file ({size} bytes, {mime or 'unknown type'}); there is no text "
            "reader for this format, so its contents were not returned."
        ),
    }


# ── The budget ───────────────────────────────────────────────────────


def _collect(chunks: Iterable[str], max_chars: int | None, sep: str = "\n\n") -> str:
    """Join ``chunks`` with ``sep``, pulling no more of them than the budget needs.

    ``chunks`` is consumed lazily, so a generator that parses one page (or
    paragraph, slide, row) per item stops parsing when this stops pulling.
    Blank chunks are skipped.
    """
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        if not chunk.strip():
            continue
        used += len(chunk) + (len(sep) if parts else 0)
        parts.append(chunk)
        if max_chars is not None and used > max_chars:
            return _stopped(sep.join(parts), max_chars)
    return sep.join(parts)


def _stopped(text: str, max_chars: int | None) -> str:
    """``text`` cut to ``max_chars`` with the stopped-early note, if it is over."""
    if max_chars is None or len(text) <= max_chars:
        return text
    return (
        f"{text[:max_chars]}\n\n{STOPPED_EARLY} {max_chars} characters; "
        "the rest of the document was not read]"
    )


# ── Per-format extractors ────────────────────────────────────────────


def _need(kind: str) -> Any:
    module = {"docx": "docx", "xlsx": "openpyxl", "pptx": "pptx"}[kind]
    try:
        return __import__(module)
    except ImportError as exc:
        raise ExtractorUnavailableError(
            f"reading {kind} files needs the {_PACKAGE[kind]} package "
            f"(pip install {_PACKAGE[kind]})"
        ) from exc


def _pdf(raw: bytes, max_chars: int | None) -> str:
    """pypdf, then ``pdftotext`` when pypdf is missing, fails or finds no text."""
    try:
        import pypdf
    except ImportError:
        return _pdf_via_pdftotext(raw, max_chars)

    def pages() -> Iterator[str]:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                yield f"[Page {i + 1}]\n{text}"

    has_poppler = shutil.which("pdftotext") is not None
    try:
        text = _collect(pages(), max_chars)
    except Exception as exc:  # noqa: BLE001 - malformed PDFs raise anything
        if not has_poppler:
            raise ExtractionError(f"could not read this pdf file: {exc}") from exc
        try:
            return _pdf_via_pdftotext(raw, max_chars)
        except ExtractionError as alt:
            raise ExtractionError(f"pypdf: {exc}; {alt}") from exc
    if text:
        return text
    if has_poppler:
        try:
            return _pdf_via_pdftotext(raw, max_chars)
        except ExtractionError:
            pass
    return _PDF_NO_TEXT


def _pdf_via_pdftotext(raw: bytes, max_chars: int | None) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        raise ExtractorUnavailableError(
            "reading pdf files needs the pypdf package (pip install pypdf) "
            "or the pdftotext command (poppler-utils)"
        )
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, bytes on stdin
            [exe, "-layout", "-", "-"],
            input=raw,
            capture_output=True,
            timeout=_PDFTOTEXT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("pdftotext timed out") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", errors="replace").strip()
        raise ExtractionError(f"pdftotext failed: {detail or done.returncode}")
    text = done.stdout.decode("utf-8", errors="replace")
    return _stopped(text, max_chars) if text.strip() else _PDF_NO_TEXT


def _as_plain_docx(raw: bytes) -> bytes:
    """A ``.docm`` package relabelled as a ``.docx`` one; anything else as is."""
    try:
        src = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return raw  # python-docx gives the clearer error
    with src:
        try:
            types = src.read("[Content_Types].xml")
        except KeyError:
            return raw
        if _DOCM_MAIN not in types:
            return raw
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    data = data.replace(_DOCM_MAIN, _DOCX_MAIN)
                dst.writestr(item, data)
    return out.getvalue()


def _docx(raw: bytes, max_chars: int | None) -> str:
    docx = _need("docx")
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(io.BytesIO(_as_plain_docx(raw)))

    def blocks(parent: Any) -> Iterator[str]:
        # Body order, so a table sits between the paragraphs it sat between.
        for child in parent.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                yield Paragraph(child, doc).text
            elif tag == "tbl":
                yield "\n".join(
                    "\t".join(cell.text.strip() for cell in row.cells)
                    for row in Table(child, doc).rows
                )
            elif tag == "sdt":
                # A body-level content control (cover page, TOC block):
                # its paragraphs and tables live in w:sdtContent.
                for inner in child.iterchildren():
                    if inner.tag.rsplit("}", 1)[-1] == "sdtContent":
                        yield from blocks(inner)

    return _collect(blocks(doc.element.body), max_chars)


def _xlsx(raw: bytes, max_chars: int | None) -> str:
    openpyxl = _need("xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)

    def lines() -> Iterator[str]:
        for n, ws in enumerate(wb.worksheets):
            yield ("\n" if n else "") + f"## Sheet: {ws.title}"
            kept = 0
            for row in ws.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                if kept >= XLSX_MAX_ROWS_PER_SHEET:
                    yield f"[... rows after {XLSX_MAX_ROWS_PER_SHEET} truncated]"
                    break
                yield "\t".join("" if v is None else str(v) for v in row)
                kept += 1

    try:
        return _collect(lines(), max_chars, sep="\n")
    finally:
        wb.close()


def _pptx(raw: bytes, max_chars: int | None) -> str:
    pptx = _need("pptx")
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = pptx.Presentation(io.BytesIO(raw))

    def shape_text(shapes: Any) -> Iterator[str]:
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shape_text(shape.shapes)
            elif getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                yield shape.text_frame.text
            elif getattr(shape, "has_table", False):
                yield from (
                    "\t".join(cell.text.strip() for cell in row.cells) for row in shape.table.rows
                )

    def lines() -> Iterator[str]:
        for i, slide in enumerate(prs.slides, start=1):
            yield ("\n" if i > 1 else "") + f"[Slide {i}]"
            yield from shape_text(slide.shapes)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    yield f"Notes: {notes}"

    return _collect(lines(), max_chars, sep="\n")


_EXTRACTORS: dict[str, Callable[[bytes, int | None], str]] = {
    "pdf": _pdf,
    "docx": _docx,
    "xlsx": _xlsx,
    "pptx": _pptx,
}
