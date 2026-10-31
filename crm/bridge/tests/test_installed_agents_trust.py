"""What the Marketplace view's trust badge reads off an installed agent.

Two facts the install already recorded in ``installed.yaml`` and the listing
dropped: the sha256 the bundle was pinned to, and -- for an agent a plugin
ships -- WHICH distribution it came from, so the view can show that plugin's
signature, scan and drift rather than inventing an answer for the agent.

Both are optional: a row that recorded neither keeps exactly the shape it had,
which ``test_installed_agents_path_security`` pins key for key.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from routers.installed_agents import list_installed_agents

SHA = "d" * 64


@pytest.fixture
def manifests(monkeypatch, tmp_path):
    root = tmp_path / "manifests"
    root.mkdir()
    for agent_id in ("note-taker", "desk-watch", "local-one"):
        (root / f"{agent_id}.yaml").write_text(f"id: {agent_id}\n")
    monkeypatch.setattr("routers.installed_agents.MANIFEST_DIR", root)
    return root


def _list(installed: dict) -> dict:
    config = MagicMock(installed_agents=installed)
    with patch("robothor.templates.instance.InstanceConfig.load", return_value=config):
        return {row["agent_id"]: row for row in list_installed_agents()["agents"]}


def test_a_hub_agent_carries_the_sha256_it_was_pinned_to(manifests) -> None:
    rows = _list({"note-taker": {"source": "hub", "version": "1.0.0", "source_sha256": SHA}})
    assert rows["note-taker"]["source_sha256"] == SHA
    assert "plugin" not in rows["note-taker"]


def test_a_plugin_agent_names_its_distribution(manifests) -> None:
    rows = _list({"desk-watch": {"source": "plugin:genus-example", "version": "0.3.0"}})
    assert rows["desk-watch"]["plugin"] == "genus-example"
    assert "source_sha256" not in rows["desk-watch"]


def test_an_agent_with_neither_keeps_its_old_shape(manifests) -> None:
    rows = _list({"local-one": {"source": "local", "version": "1"}})
    assert "plugin" not in rows["local-one"]
    assert "source_sha256" not in rows["local-one"]


def test_a_recorded_value_that_is_not_a_sha256_is_not_passed_on(manifests) -> None:
    """``installed.yaml`` is mutable instance data. A value that is not 64 hex
    characters is not a pin, and the badge must not render it as one."""
    rows = _list({"note-taker": {"source": "hub", "version": "1", "source_sha256": "<b>x</b>"}})
    assert "source_sha256" not in rows["note-taker"]


def test_a_plugin_source_that_is_not_a_distribution_name_is_not_passed_on(manifests) -> None:
    rows = _list({"desk-watch": {"source": "plugin:../../etc", "version": "1"}})
    assert "plugin" not in rows["desk-watch"]
