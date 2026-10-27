"""`genus agent publish` points the operator at the hub it published to.

The "View at" link was once hardcoded to a parked domain, so it was wrong for
every operator and ignored ``PROGRAMMATIC_RESOURCES_URL``.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from robothor.cli.agent import _cmd_agent_publish
from robothor.templates import hub_client

if TYPE_CHECKING:
    import pytest


def test_publish_links_to_the_configured_hub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hub_client, "HUB_BASE_URL", "https://hub.example/")
    monkeypatch.setenv("PROGRAMMATIC_RESOURCES_API_KEY", "test-key")
    submitted: list[str] = []

    def fake_submit(self: hub_client.HubClient, repo_url: str) -> dict[str, str]:
        submitted.append(repo_url)
        return {"name": "My Agent", "slug": "my-agent"}

    monkeypatch.setattr(hub_client.HubClient, "submit", fake_submit)

    code = _cmd_agent_publish(argparse.Namespace(repo_url="https://github.com/example/my-agent"))

    assert code == 0
    assert submitted == ["https://github.com/example/my-agent"]
    out = capsys.readouterr().out
    assert "Published: My Agent (my-agent)" in out
    assert "View at: https://hub.example/bundle/my-agent" in out
