"""Nothing the platform ships may arrive with an approval gate switched on.

2026-09-17. `crm-steward`'s shipped manifest template carried
``v2.guardrails: [human_approval]`` and ``human_approval_tools:
[delete_person]``. Every instance installing that template inherited a nightly,
unattended hygiene scan that stopped at each duplicate contact to ask a person,
waited out ``human_approval_timeout`` and was then denied — so the run deleted
nothing and the operator got one Telegram prompt per duplicate.

Genus OS runs agents autonomously by design. The approval gate is a feature an
instance may opt into, per agent, for the tools it chooses; a template that
ships it turns an opt-in into a default for everyone who installs it. The gate
therefore lives in these files only as a COMMENTED example.

A grep, not a schema rule: the manifest schema must keep accepting the keys —
they are supported configuration — and what this test pins is that the shipped
files do not USE them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]

#: Every manifest the platform ships, plus the schema doc whose examples an
#: operator copies. `docs/agents/*.yaml` is instance data and gitignored, so
#: `schema.yaml` is the one file under it that belongs to the platform.
_SHIPPED = sorted(_REPO.glob("templates/**/*.yaml")) + [_REPO / "docs" / "agents" / "schema.yaml"]

#: The sentence a commented example must carry, so a reader copying it out
#: knows what they are turning on.
_OPT_IN_SENTENCE = "opt-in; the default is autonomous"


def _is_field_definition(value: object) -> bool:
    """``{type: list[string], default: [], description: ...}`` — `schema.yaml`
    DESCRIBING the key, not a manifest setting it. Documenting supported
    configuration is the point of that file; what this test forbids is a
    shipped file switching the gate on."""
    return isinstance(value, dict) and "type" in value and "default" in value


def _active_approval_keys(document: object, trail: str = "") -> set[str]:
    """Paths of every `human_approval*` key with a value that DOES something.

    An empty list or a false flag is the schema stating its default, not a
    template arming a gate.
    """
    found: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            path = f"{trail}.{key}" if trail else str(key)
            if str(key).startswith("human_approval") and value and not _is_field_definition(value):
                found.add(path)
            found |= _active_approval_keys(value, path)
    elif isinstance(document, list):
        for index, item in enumerate(document):
            found |= _active_approval_keys(item, f"{trail}[{index}]")
        if trail.endswith("guardrails") and "human_approval" in document:
            found.add(f"{trail}: human_approval")
    return found


def _load(path: Path) -> object:
    """Jinja stripped, the way `genus init` renders it, so the YAML parses."""
    import re

    text = re.sub(r"\{\%.*?\%\}", "", path.read_text(), flags=re.DOTALL)
    text = re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", "placeholder", text)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


@pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_shipped_file_arms_an_approval_gate(path: Path) -> None:
    active = sorted(_active_approval_keys(_load(path)))

    assert not active, (
        f"{path.relative_to(_REPO)} ships an armed approval gate at {active}. "
        "The platform runs agents autonomously; move it to a commented example "
        f"carrying '{_OPT_IN_SENTENCE}'."
    )


def test_the_commented_examples_say_the_gate_is_opt_in() -> None:
    """A commented-out key with no explanation is an invitation to uncomment
    it. Wherever a shipped file still SHOWS the gate, it says what it is."""
    offenders: list[str] = []
    for path in _SHIPPED:
        if not path.exists():
            continue
        text = path.read_text()
        commented = [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("#") and "human_approval" in line
        ]
        if commented and _OPT_IN_SENTENCE not in text:
            offenders.append(str(path.relative_to(_REPO)))

    assert not offenders, (
        f"{offenders} show the approval gate without saying it is optional; "
        f"add the sentence '{_OPT_IN_SENTENCE}'."
    )


def test_the_grep_would_catch_a_regression() -> None:
    """A gate test that passes because its scanner sees nothing is the defect
    it exists to prevent."""
    armed = {
        "v2": {
            "guardrails": ["write_path_restrict", "human_approval"],
            "human_approval_tools": ["delete_person"],
        }
    }

    assert _active_approval_keys(armed) == {
        "v2.human_approval_tools",
        "v2.guardrails: human_approval",
    }
    assert _active_approval_keys({"v2": {"human_approval_tools": [], "guardrails": []}}) == set()
    assert (
        _active_approval_keys(
            {"optional": {"human_approval_tools": {"type": "list[string]", "default": []}}}
        )
        == set()
    ), "a schema DESCRIBING the key is not a manifest setting it"
