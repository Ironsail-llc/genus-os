"""Every schedule the dashboard suggests is one the server accepts.

The workspace settings drawer and the new-workspace dialog show example
schedules as placeholders. A placeholder the engine's parser refuses teaches
the user to type something that fails on save, so each one is read out of the
component source and run through the workspace schedule check
(``robothor.rooms.definition``), minimum interval included.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "app" / "src" / "components" / "rooms"

#: ``data-schedule-example`` marks an element whose placeholder is a schedule.
_EXAMPLE = re.compile(r'placeholder="([^"]+)"\s*\n?\s*data-schedule-example')


def _examples() -> list[tuple[str, str]]:
    return [
        (path.name, match.group(1))
        for path in sorted(COMPONENTS.glob("*.tsx"))
        for match in _EXAMPLE.finditer(path.read_text(encoding="utf-8"))
    ]


def test_the_settings_drawer_marks_its_schedule_example() -> None:
    assert any(name == "room-settings-drawer.tsx" for name, _ in _examples()), (
        "the schedule input's placeholder must carry data-schedule-example"
    )


@pytest.mark.parametrize(("component", "example"), _examples())
def test_each_schedule_example_parses(component: str, example: str) -> None:
    from robothor.rooms import definition

    for text in (part.strip() for part in example.split(" or ")):
        definition._parse_schedule(text, "UTC")
