"""The full-stack demo's docker-compose.yml must install a real package.

`examples/full-stack/docker-compose.yml` used to run
`pip install --quiet robothor[api]` in the `robothor` service's startup
command. There has never been a `robothor` package on PyPI -- the
distribution is published as `genusos` -- so `docker compose up` failed at
container startup for anyone following the example.

This test YAML-parses the compose file and asserts no service's command does
a `pip install` of the bare `robothor` package name.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "examples" / "full-stack" / "docker-compose.yml"

# `[^&\n]*` stops the scan at the next `&&` shell-chain boundary (or a
# newline) so a legitimate later command in the same chain -- e.g. the
# `robothor serve` CLI invocation -- can't be mistaken for part of the
# `pip install` argument list.
_BARE_ROBOTHOR_PIP_INSTALL = re.compile(r"pip install[^&\n]*\brobothor\b")


def _service_commands() -> list[str]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    commands = []
    for service in compose.get("services", {}).values():
        command = service.get("command")
        if isinstance(command, str):
            commands.append(command)
        elif isinstance(command, list):
            commands.append(" ".join(str(part) for part in command))
    return commands


def test_compose_file_parses_and_has_services():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    assert "services" in compose
    assert "robothor" in compose["services"]


def test_no_service_pip_installs_the_nonexistent_bare_robothor_package():
    commands = _service_commands()
    assert commands, "expected at least one service with a command"
    offenders = [cmd for cmd in commands if _BARE_ROBOTHOR_PIP_INSTALL.search(cmd)]
    assert not offenders, (
        "docker-compose.yml pip-installs the nonexistent PyPI package "
        f"'robothor'; install 'genusos' instead: {offenders}"
    )
