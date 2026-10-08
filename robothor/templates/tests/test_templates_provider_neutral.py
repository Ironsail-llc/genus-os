"""Shipped agent templates reach mail and calendar only through the tools.

2026-10-07. The core routes the ``gws_gmail_*`` and ``gws_calendar_*`` tools by
``workspace_provider`` (google | microsoft365), and every mail and calendar
guard — do-not-contact, no-auto scheduling, dedup, duplicate-reply,
inbound-only, verification read-backs — hangs off those tool names. Several
shipped templates still told the agent to run the Google CLIs (``gog …``,
``gws gmail …``, ``gws calendar …``) through ``exec``. On a Microsoft 365
instance there is no Google CLI, so those agents broke; on a Google instance the
CLI route went around every guard the tools carry.

A grep, not a schema rule: the instruction text is free prose, and what this
ratchet pins is that no shipped file names a Google CLI invocation or the
Google API host. Manifest ``change:`` lines are history — an entry that says
"drop the gog fallback" records the removal, it does not ask for the call —
so they are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

#: A Google CLI invocation, or the Google API host an agent would curl.
_DIRECT_GOOGLE = re.compile(r"\bgog\b|\bgws\s+(?:gmail|calendar|chat)\b|googleapis\.com")

#: A manifest changelog line: history, not an instruction.
_CHANGELOG_LINE = re.compile(r"^\s*(?:-\s*)?change:")

_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".j2", ".jinja", ".tmpl", ".json"}


def shipped_files(repo: Path = _REPO) -> list[Path]:
    """Every template file the platform ships, plus the manifest schema whose
    examples an operator copies into a manifest."""
    files = [
        p for p in (repo / "templates").rglob("*") if p.is_file() and p.suffix in _TEXT_SUFFIXES
    ]
    schemas = (
        repo / "docs" / "agents" / "schema.yaml",
        repo / "robothor" / "engine" / "schema" / "agent_manifest.yaml",
    )
    files.extend(schema for schema in schemas if schema.is_file())
    return sorted(files)


def direct_google_calls(repo: Path = _REPO) -> list[str]:
    """``file:line: text`` for every direct Google CLI or API reference."""
    hits: list[str] = []
    for path in shipped_files(repo):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CHANGELOG_LINE.match(line):
                continue
            if _DIRECT_GOOGLE.search(line):
                hits.append(f"{path.relative_to(repo)}:{number}: {line.strip()}")
    return hits


def test_the_scan_sees_the_shipped_templates() -> None:
    """A scan over nothing reports nothing — prove it is reading real files."""
    names = {p.relative_to(_REPO).as_posix() for p in shipped_files()}
    assert "templates/agents/email/email-classifier/instructions.template.md" in names
    assert "templates/agents/calendar/calendar-monitor/manifest.template.yaml" in names
    assert len(names) > 20


def test_the_pattern_catches_each_cli_form() -> None:
    for line in (
        "exec: gog calendar events owner@example.com --from today",
        "gws gmail users messages list",
        "gws calendar events insert",
        "gws chat spaces list",
        "curl https://www.googleapis.com/calendar/v3/",
        '    - "^gog calendar"',
    ):
        assert _DIRECT_GOOGLE.search(line), line
    for line in (
        "gws_calendar_list(time_min=...)",
        "gws_gmail_modify(message_id=..., remove_labels=['UNREAD'])",
        '    change: "Drop the gog CLI fallback."',
    ):
        assert not _DIRECT_GOOGLE.search(line) or _CHANGELOG_LINE.match(line), line


def test_no_shipped_template_calls_a_google_cli_directly() -> None:
    hits = direct_google_calls()
    assert not hits, (
        "Shipped templates must reach mail and calendar through the provider-neutral "
        "gws_gmail_* / gws_calendar_* tools, never a Google CLI or googleapis.com "
        "(breaks on Microsoft 365 and bypasses the tool guards):\n  " + "\n  ".join(hits)
    )
