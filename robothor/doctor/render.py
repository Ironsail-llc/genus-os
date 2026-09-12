"""How a report reads in a terminal.

Two rules. Failures must be findable in a wall of green -- the table is grouped
by category and every line carries its severity -- and the summary must say what
to do next, because the most common reading of a diagnostic is by someone who
has just been woken up.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.runner import DoctorReport

__all__ = ["render_json", "render_text"]

#: Kept in sync with ``genus config validate``'s old glyphs so an operator who
#: has read one has read the other. ``skip`` is a dot, not a tick: a check that
#: did not run has not passed.
_GLYPH = {
    "pass": "\033[32m✓\033[0m",
    "fail": "\033[31m✗\033[0m",
    "skip": "·",
}

_SEVERITY_LABEL = {"required": "", "recommended": " (recommended)", "info": " (info)"}


def render_json(report: DoctorReport) -> str:
    """The machine-readable report. Same keys as ``GET /api/doctor``."""
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def render_text(report: DoctorReport) -> str:
    """The human table."""
    if report.errored:
        return f"genus doctor could not run: {report.error_detail}"

    lines: list[str] = []
    category = ""
    for row in report.results:
        if row.category != category:
            category = row.category
            lines.append(f"\n{category}")
        label = _SEVERITY_LABEL.get(row.severity, "")
        glyph = _GLYPH.get(row.status, "?")
        detail = f": {row.detail}" if row.detail else ""
        lines.append(f"  {glyph} {row.id}{label}{detail}")

    summary = report.summary
    lines.append("")
    lines.append(
        f"{summary['passed']} passed, {summary['required_failed']} failed, "
        f"{summary['recommended_failed']} recommended, {summary['skipped']} skipped"
    )
    fixable = [row.id for row in report.results if row.status == "fail" and row.fixable]
    if fixable:
        lines.append(f"Repairable with 'genus doctor --fix': {', '.join(fixable)}")
    if summary["required_failed"]:
        lines.append("This instance is not fully working; the ✗ lines above say why.")
    return "\n".join(lines).lstrip("\n")
