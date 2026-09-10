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


def test_no_service_seeds_the_schema_through_docker_entrypoint_initdb():
    """A frozen SQL snapshot mounted into initdb is a migration path of its own.

    It runs outside `schema_migrations_v2`, so the ledger stays empty while the
    schema exists — the state that makes the next `robothor migrate` look like
    it must replay 001_init.sql over live data.
    """
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    mounts = []
    for name, service in compose.get("services", {}).items():
        for volume in service.get("volumes", []) or []:
            if isinstance(volume, str) and "docker-entrypoint-initdb.d" in volume:
                mounts.append((name, volume))
    assert not mounts, f"initdb schema snapshot is still mounted: {mounts}"

    assert not (COMPOSE_PATH.parent / "init-db.sql").exists(), (
        "examples/full-stack/init-db.sql is a frozen copy of the baseline migration"
    )


def test_a_one_shot_migrate_service_runs_the_canonical_migrator():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    assert "migrate" in services, "expected a one-shot 'migrate' service"

    migrate = services["migrate"]
    command = migrate["command"]
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    assert "robothor.cli" in command and "migrate" in command, command

    assert migrate.get("restart") == "no"
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_api_service_waits_for_migrations_to_complete():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    depends_on = compose["services"]["robothor"]["depends_on"]
    assert depends_on["migrate"]["condition"] == "service_completed_successfully"
