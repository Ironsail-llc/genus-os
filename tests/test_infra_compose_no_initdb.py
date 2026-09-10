"""The infra stack must not seed the schema behind the migrator's back.

`infra/docker-compose.yml` used to mount `./migrations` into
`docker-entrypoint-initdb.d`, so PostgreSQL ran `001_init.sql` on first boot —
outside `schema_migrations_v2`. The result is a database whose schema exists
while the ledger is empty, which is indistinguishable from a virgin database
unless the migrator is told otherwise (`robothor migrate --adopt-baseline`).
Only `robothor migrate` may create schema.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"


def test_no_infra_service_mounts_migrations_into_initdb():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())

    offenders = []
    for name, service in compose.get("services", {}).items():
        for volume in service.get("volumes", []) or []:
            if isinstance(volume, str) and "docker-entrypoint-initdb.d" in volume:
                offenders.append((name, volume))

    assert not offenders, (
        "migrations are mounted into docker-entrypoint-initdb.d, which applies "
        f"them outside the canonical ledger: {offenders}"
    )
