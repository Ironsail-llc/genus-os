"""An agent can learn a block's props from the tool, and from a refusal.

A run that publishes an edition has one tool and, often, no way to look
anything up. The tool's description has to say which props each component
takes, and a refused block has to name the props it accepts, or the agent
guesses and gives up.
"""

from __future__ import annotations

import re

import pytest

from robothor.engine.tools.schemas import get_engine_schemas
from robothor.rooms.editions import COMPONENT_CHECKS, InvalidBlocks, validate_blocks


def _description() -> str:
    return str(get_engine_schemas()["room_publish_edition"]["function"]["description"])


def _accepted(component: str) -> list[str]:
    """The props a refusal says ``component`` accepts."""
    with pytest.raises(InvalidBlocks) as refused:
        validate_blocks([{"component": component, "props": {"zz_unknown": 1}}])
    message = str(refused.value)
    assert "unknown props zz_unknown" in message
    found = re.search(r"accepts: ([^;.]+)", message)
    assert found, message
    return [name.strip().removesuffix(" (required)") for name in found.group(1).split(",")]


@pytest.mark.parametrize("component", sorted(COMPONENT_CHECKS))
def test_a_refusal_names_the_props_the_component_accepts(component: str) -> None:
    accepted = _accepted(component)
    assert accepted, component


def test_a_refusal_marks_the_required_props() -> None:
    with pytest.raises(InvalidBlocks) as refused:
        validate_blocks([{"component": "render_markdown", "props": {"text": "hi"}}])
    message = str(refused.value)
    assert "unknown props text" in message
    assert "content (required)" in message
    assert "title" in message


@pytest.mark.parametrize("component", sorted(COMPONENT_CHECKS))
def test_the_tool_description_lists_each_components_props(component: str) -> None:
    description = _description()
    assert component in description
    start = description.index("Props")
    section = description[start:]
    part = section[section.index(component + " {") :]
    # Cut at the component's own entry: the next one starts at "; render_".
    part = part.split("; render_")[0]
    for prop in _accepted(component):
        assert re.search(rf"\b{re.escape(prop)}\b", part), (component, prop)


def test_a_missing_required_prop_also_names_what_is_accepted() -> None:
    with pytest.raises(InvalidBlocks) as refused:
        validate_blocks([{"component": "render_markdown", "props": {"title": "x"}}])
    message = str(refused.value)
    assert "missing props content" in message
    assert "accepts:" in message


def test_a_refused_item_inside_a_block_says_which_item() -> None:
    """The accepted list of a nested item must not read as the block's own props."""
    with pytest.raises(InvalidBlocks) as grid:
        validate_blocks(
            [
                {
                    "component": "render_metric_grid",
                    "props": {"metrics": [{"title": "a", "value": 1}, {"label": "b", "value": 2}]},
                }
            ]
        )
    assert "metrics[1]: unknown props label; accepts:" in str(grid.value)

    with pytest.raises(InvalidBlocks) as timeline:
        validate_blocks(
            [{"component": "render_timeline", "props": {"events": [{"id": "1", "title": "t"}]}}]
        )
    assert "events[0]: missing props timestamp; accepts:" in str(timeline.value)


def test_a_blocks_own_props_carry_no_item_prefix() -> None:
    with pytest.raises(InvalidBlocks) as refused:
        validate_blocks([{"component": "render_metric_card", "props": {"label": "x"}}])
    assert "(render_metric_card): unknown props label; accepts:" in str(refused.value)
