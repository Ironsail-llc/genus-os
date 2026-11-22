"""Every schedule a person is shown as an example is one the server accepts.

A workspace's schedule is read by one parser (``robothor.engine.cron_parse``,
through ``robothor.rooms.definition``). An example it refuses teaches people
to type something that fails on save, so every example shown anywhere is
collected here and parsed:

* the dashboard: an input's placeholder marked ``data-schedule-example``;
* the bridge: the examples quoted (“…”) in its ``invalid_schedule`` message;
* the agent: the examples quoted ('…') in the workspace tools' schedule
  descriptions;
* the docs: the Examples column of the user guide's schedule table, and any
  backticked schedule in the tools reference's workspace section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "app" / "src" / "components" / "rooms"

#: ``data-schedule-example`` marks an element whose placeholder is a schedule.
_PLACEHOLDER = re.compile(r'placeholder="([^"]+)"\s*\n?\s*data-schedule-example')
_CURLY = re.compile(r"“([^”]+)”")
_SINGLE = re.compile(r"'([^']+)'")
_TICKS = re.compile(r"`([^`]+)`")
#: What a schedule looks like, to pick schedules out of prose.
_SCHEDULE_LIKE = re.compile(r"^(every\b.*|[\d*/,-]+( [\d*/,-]+){4})$", re.IGNORECASE)


def _dashboard() -> list[tuple[str, str]]:
    found = []
    for path in sorted(COMPONENTS.glob("*.tsx")):
        for match in _PLACEHOLDER.finditer(path.read_text(encoding="utf-8")):
            found += [(f"dashboard {path.name}", part.strip()) for part in match[1].split(" or ")]
    return found


def _bridge() -> list[tuple[str, str]]:
    # Read from source: importing the router would put crm/bridge (and its own
    # ``tests`` package) on the path of this suite.
    source = (ROOT / "crm" / "bridge" / "routers" / "rooms.py").read_text(encoding="utf-8")
    block = source.split('"invalid_schedule": (', 1)[1].split("),", 1)[0]
    return [("bridge invalid_schedule", t) for t in _CURLY.findall(block)]


def _tools() -> list[tuple[str, str]]:
    from robothor.engine.tools.schemas import get_engine_schemas

    found = []
    for name, schema in sorted(get_engine_schemas().items()):
        if not name.startswith("room_"):
            continue
        params = schema["function"]["parameters"].get("properties", {})
        for key in ("schedule", "schedule_text"):
            text = str(params.get(key, {}).get("description", ""))
            found += [(f"tool {name}.{key}", t) for t in _SINGLE.findall(text)]
    return found


def _guide() -> list[tuple[str, str]]:
    text = (ROOT / "docs" / "WORKSPACES.md").read_text(encoding="utf-8")
    section = text.split("### Schedule", 1)[1].split("\n#", 1)[0]
    found = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 2 and not set(cells[-1]) <= {"-", " "}:
            found += [("docs/WORKSPACES.md", t) for t in _TICKS.findall(cells[-1])]
    return found


def _tools_reference() -> list[tuple[str, str]]:
    text = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    start = text.find("room_create")
    section = text[start : start + 20000] if start >= 0 else ""
    return [
        ("docs/TOOLS.md", t) for t in _TICKS.findall(section) if _SCHEDULE_LIKE.match(t.strip())
    ]


def _examples() -> list[tuple[str, str]]:
    return _dashboard() + _bridge() + _tools() + _guide() + _tools_reference()


def test_every_place_that_shows_schedules_shows_some() -> None:
    sources = {source.split(" ")[0] for source, _ in _examples()}
    assert {"dashboard", "bridge", "tool", "docs/WORKSPACES.md"} <= sources, sources


@pytest.mark.parametrize(("source", "example"), _examples())
def test_each_schedule_example_parses(source: str, example: str) -> None:
    from robothor.rooms import definition

    definition._parse_schedule(example, "UTC")
