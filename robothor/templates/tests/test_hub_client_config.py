"""Which marketplace host the hub client talks to.

The default once pointed at a parked domain, so search, install and publish
all failed with no hint that the host itself was wrong. ``HUB_BASE_URL`` is
read when the module is imported, so the default and the override are checked
in a fresh interpreter rather than by reloading the module under other tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from robothor.templates import hub_client
from robothor.templates.hub_client import HubClient

if TYPE_CHECKING:
    import pytest

DEFAULT_HUB = "https://programmaticresources.io"
PROBE = (
    "from robothor.templates.hub_client import HubClient; print(HubClient(api_key='k').base_url)"
)


def _base_url_in_fresh_interpreter(env_value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PROGRAMMATIC_RESOURCES_URL"}
    if env_value is not None:
        env["PROGRAMMATIC_RESOURCES_URL"] = env_value
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_default_hub_is_the_live_marketplace() -> None:
    assert _base_url_in_fresh_interpreter(None) == DEFAULT_HUB


def test_environment_override_replaces_the_default() -> None:
    assert _base_url_in_fresh_interpreter("https://hub.example/") == "https://hub.example"


def test_client_reads_the_module_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_client, "HUB_BASE_URL", "https://hub.example/")
    assert HubClient(api_key="k").base_url == "https://hub.example"
