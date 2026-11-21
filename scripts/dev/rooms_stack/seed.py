"""Seed the workspaces dry run: two generic companies and their people.

Development only (``scripts/dev/rooms-stack.sh seed``). Idempotent: an
account that exists keeps its id and gets its password set again.

* ``dryrun-holding`` (the platform tenant): Owen, its owner (an instance with
  no owner is sent to first-run setup), and Hal, its admin.
* ``dryrun-company``, a child of it: Alice and Bob (members), Vera (viewer),
  Dana (admin) and Carol (member, shared nothing). Hal reaches it only by
  switching in.

Every account signs in by email (``<name>@example.com``) with the password
given as the first argument. Prints the accounts as JSON for the Playwright
specs.

    seed.py PASSWORD [HOLDING COMPANY]
"""

from __future__ import annotations

import json
import sys

HOLDING = sys.argv[2] if len(sys.argv) > 2 else "dryrun-holding"
COMPANY = sys.argv[3] if len(sys.argv) > 3 else "dryrun-company"

PEOPLE = {
    "alice": (COMPANY, "member", "Alice"),
    "bob": (COMPANY, "member", "Bob"),
    "vera": (COMPANY, "viewer", "Vera"),
    "dana": (COMPANY, "admin", "Dana"),
    "carol": (COMPANY, "member", "Carol"),
    "hal": (HOLDING, "admin", "Hal"),
    "owen": (HOLDING, "owner", "Owen"),
}


def _refuse_outside_a_dry_run() -> None:
    from robothor.settings import get_settings

    settings = get_settings()
    if settings.auth.environment != "development" or not settings.database.name.endswith("_test"):
        sys.exit("rooms stack: refusing to seed outside development on a *_test database")


def _tenants() -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        for tenant_id, name, parent in (
            (HOLDING, "Holding (dry run)", None),
            (COMPANY, "Company (dry run)", HOLDING),
        ):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name, "
                "parent_tenant_id = EXCLUDED.parent_tenant_id, active = TRUE",
                (tenant_id, name, parent),
            )
        conn.commit()


def _account(tenant_id: str, email: str, role: str, name: str) -> dict[str, str]:
    from robothor.auth import accounts

    row = accounts.create_account(
        tenant_id=tenant_id, email=email, display_name=name, role=role, status="active"
    ) or accounts.get_account_by_email(tenant_id, email)
    if not row:
        sys.exit(f"rooms stack: could not create or find {email}")
    return {"id": str(row["id"]), "tenant": tenant_id, "role": role, "name": name}


def main() -> None:
    _refuse_outside_a_dry_run()
    from robothor.auth import local_login

    password = sys.argv[1] if len(sys.argv) > 1 else ""
    if len(password) < local_login.MIN_PASSWORD_LENGTH:
        sys.exit("rooms stack: give a password of 12 characters or more")
    _tenants()
    people = {}
    for key, (tenant_id, role, name) in PEOPLE.items():
        email = f"{key}@example.com"
        person = _account(tenant_id, email, role, name)
        local_login.set_password(person["id"], password)
        people[key] = {**person, "email": email}
    print(json.dumps({"holding": HOLDING, "company": COMPANY, "people": people}, indent=2))


if __name__ == "__main__":
    main()
