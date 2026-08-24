"""A fresh install must be able to run its own agents.

`check_tool_permission` fails closed when a role has no rules: it returns
"No permission rules for role 'service' — access denied". Every
system-triggered run (cron, hook, workflow, sub-agent) carries the default
`service_role` of "service", and the shipped systemd drop-in enables RBAC in
`enforce`.

Migration 037 seeds viewer/user/admin/owner/member/guest and not `service`.
Production has the rule only because someone inserted it by hand on
2026-07-02 — `infra/flags.yaml` still records the symptom as "46 blocks day
one". Until 107 that repair lived in one database instead of in the platform,
so a clean install turned RBAC on and denied every scheduled agent every
tool.

This is a source-level test on purpose. The defect is the absence of a line
in the migration chain, and a database fixture that happens to have the row
(from a hand-fix, or from an earlier run of this very migration) would report
health it did not verify.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "crm" / "migrations"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"

#: The literal default in AgentConfig.service_role. Both sides of the gate
#: have to agree on the spelling, and a rename that touched only one of them
#: would restore the outage this test exists to prevent.
SERVICE_ROLE = "service"


def _seeded_roles() -> set[str]:
    """Roles that any migration inserts into role_permissions."""
    roles: set[str] = set()
    pattern = re.compile(r"\(\s*'__default__'\s*,\s*'([a-z_]+)'", re.IGNORECASE)
    for sql_file in MIGRATIONS.glob("*.sql"):
        text = sql_file.read_text(encoding="utf-8")
        if "role_permissions" not in text:
            continue
        roles.update(match.group(1).lower() for match in pattern.finditer(text))
    return roles


def test_the_service_role_is_seeded_by_a_migration() -> None:
    assert SERVICE_ROLE in _seeded_roles(), (
        "no migration grants the 'service' role, so a fresh install with RBAC "
        "in enforce denies every cron/hook/workflow agent every tool"
    )


def test_the_seeding_migration_is_in_the_canonical_manifest() -> None:
    """A migration outside the manifest never runs — which is how this class
    of defect survives being 'fixed'."""
    entries = MANIFEST.read_text(encoding="utf-8").split()
    assert any(e.endswith("107_seed_service_role.sql") for e in entries)


def test_the_default_service_role_string_still_matches() -> None:
    """If AgentConfig's default is renamed, the seeded rule stops applying and
    the gate silently starts denying again."""
    models = (REPO / "robothor" / "engine" / "models.py").read_text(encoding="utf-8")
    assert re.search(rf'service_role:\s*str\s*=\s*"{SERVICE_ROLE}"', models), (
        "AgentConfig.service_role no longer defaults to "
        f"'{SERVICE_ROLE}' — migration 107 seeds a role nothing uses"
    )
