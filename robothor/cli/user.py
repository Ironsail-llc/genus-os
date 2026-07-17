"""``robothor user`` — closed-allowlist registration.

Unknown senders on a closed channel (e.g. Telegram, Task 4) are refused with
an operator hint that spells out a ``robothor user add`` invocation. This
module makes that command real: it registers a human into the platform's
identity graph with full linkage —

    crm_people          canonical identity (one row per human)
    tenant_users         (telegram) role/tenant membership binding
    contact_identifiers   channel -> person map (telegram, email), plus the
                          memory_entity_id bridge into the memory graph
    user_accounts         (optional) SSO invite, armed via a follow-up
                          ``robothor auth grant-binding``

``crm_people`` is the canonical identity; the rest are bindings onto it. See
root ``CLAUDE.md`` rule 12 and ``crm/CLAUDE.md`` for the owner-resolution
context this plugs into.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

# Mirrors robothor.auth.tokens._HUMAN_ROLES -- the single source of truth for
# valid human roles (token scopes / role_permissions). Duplicated here (that
# name is module-private) rather than imported; keep in sync if it changes --
# see test_valid_roles_matches_platform_human_roles.
VALID_ROLES = frozenset({"owner", "admin", "member", "user", "viewer", "auditor"})


def cmd_user(args: Namespace) -> int:
    command = getattr(args, "user_command", None)
    if command == "list":
        return _cmd_list(args)
    if command == "add":
        return _cmd_add(args)
    if command == "link":
        return _cmd_link(args)
    if command == "link-face":
        return _cmd_link_face(args)
    print("usage: robothor user {list,add,link,link-face}")
    return 1


def _validate_role(role: str) -> bool:
    return role in VALID_ROLES


def _find_person_by_email(tenant_id: str, email: str) -> str | None:
    """Direct SQL: no ``crm.dal`` helper looks up a person by email alone
    (``search_people`` is name-ILIKE only)."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM crm_people WHERE tenant_id = %s AND email = %s "
            "AND deleted_at IS NULL LIMIT 1",
            (tenant_id, email),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


# ─── list ────────────────────────────────────────────────────────────────


def _cmd_list(args: Namespace) -> int:
    from robothor.db.connection import get_connection

    tenant = getattr(args, "tenant", None)
    select = """
        SELECT tu.telegram_user_id, tu.display_name, tu.role, tu.tenant_id,
               tu.is_active, tu.person_id,
               (SELECT ua.email FROM user_accounts ua
                 WHERE ua.tenant_id = tu.tenant_id AND ua.person_id = tu.person_id
                 ORDER BY ua.created_at ASC LIMIT 1) AS email
        FROM tenant_users tu
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant:
            cur.execute(select + " WHERE tu.tenant_id = %s ORDER BY tu.tenant_id, tu.id", (tenant,))
        else:
            cur.execute(select + " ORDER BY tu.tenant_id, tu.id")
        rows = cur.fetchall()

    if not rows:
        print("No tenant users found.")
        return 0

    header = (
        f"{'TELEGRAM ID':<15} {'NAME':<24} {'ROLE':<10} {'TENANT':<20} "
        f"{'ACTIVE':<7} {'LINKED':<7} {'EMAIL'}"
    )
    print(header)
    print("-" * len(header))
    for telegram_id, name, role, tenant_id, is_active, person_id, email in rows:
        print(
            f"{(telegram_id or '-'):<15} {(name or '-'):<24} {(role or '-'):<10} "
            f"{tenant_id:<20} {('yes' if is_active else 'no'):<7} "
            f"{('y' if person_id else 'n'):<7} {email or '-'}"
        )
    return 0


# ─── add ─────────────────────────────────────────────────────────────────


def _cmd_add(args: Namespace) -> int:
    import psycopg2

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal as crm_dal
    from robothor.crm.validation import validate_person_input
    from robothor.db.connection import get_connection

    role = args.role
    if not _validate_role(role):
        print(
            f"error: --role must be one of {', '.join(sorted(VALID_ROLES))}",
            file=sys.stderr,
        )
        return 2

    person_id_arg = getattr(args, "person_id", None)
    create_person_flag = bool(getattr(args, "create_person", False))
    if person_id_arg and create_person_flag:
        print("error: --person-id and --create-person are mutually exclusive", file=sys.stderr)
        return 2

    tenant = getattr(args, "tenant", None) or DEFAULT_TENANT
    name = args.name.strip()
    email = (getattr(args, "email", None) or "").strip() or None
    telegram_id = getattr(args, "telegram_id", None)

    if not crm_dal.get_tenant(tenant):
        print(
            f"error: tenant '{tenant}' does not exist — create it first with "
            "`robothor tenant create`",
            file=sys.stderr,
        )
        return 1

    # ── Resolve / create the crm_people row (canonical identity) ──
    person_created = False
    if person_id_arg:
        existing = crm_dal.get_person(person_id_arg, tenant_id=tenant)
        if not existing:
            print(f"error: person {person_id_arg} not found in tenant '{tenant}'", file=sys.stderr)
            return 1
        resolved_person_id = person_id_arg
    else:
        parts = name.split(None, 1)
        first = parts[0] if parts else name
        last = parts[1] if len(parts) > 1 else ""

        valid, reason = validate_person_input(first, last, email)
        if not valid:
            print(f"error: {reason}", file=sys.stderr)
            return 1

        found_id = None
        if not create_person_flag and email:
            found_id = _find_person_by_email(tenant, email)

        if found_id:
            resolved_person_id = found_id
        else:
            created_id = crm_dal.create_person(first, last, email=email, tenant_id=tenant)
            if not created_id:
                print(
                    "error: failed to create CRM person (blocked name or invalid input)",
                    file=sys.stderr,
                )
                return 1
            resolved_person_id = created_id
            person_created = True

    # ── tenant_users + contact_identifiers + user_accounts, one transaction ──
    identifiers_created: list[str] = []
    account_created = False
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO tenant_users
                    (telegram_user_id, display_name, tenant_id, role, person_id, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                RETURNING id, user_id
                """,
                (str(telegram_id) if telegram_id else None, name, tenant, role, resolved_person_id),
            )
            tenant_user_id, stable_user_id = cur.fetchone()

            if telegram_id:
                cur.execute(
                    """
                    INSERT INTO contact_identifiers
                        (tenant_id, channel, identifier, display_name, person_id)
                    VALUES (%s, 'telegram', %s, %s, %s)
                    ON CONFLICT (tenant_id, channel, identifier) DO NOTHING
                    RETURNING id
                    """,
                    (tenant, str(telegram_id), name, resolved_person_id),
                )
                if cur.fetchone():
                    identifiers_created.append(f"telegram:{telegram_id}")

            if email:
                cur.execute(
                    """
                    INSERT INTO contact_identifiers
                        (tenant_id, channel, identifier, display_name, person_id)
                    VALUES (%s, 'email', %s, %s, %s)
                    ON CONFLICT (tenant_id, channel, identifier) DO NOTHING
                    RETURNING id
                    """,
                    (tenant, email, name, resolved_person_id),
                )
                if cur.fetchone():
                    identifiers_created.append(f"email:{email}")

                cur.execute(
                    """
                    INSERT INTO user_accounts
                        (tenant_id, email, display_name, role, status, person_id)
                    VALUES (%s, %s, %s, %s, 'invited', %s)
                    ON CONFLICT (tenant_id, email) DO NOTHING
                    RETURNING id
                    """,
                    (tenant, email, name, role, resolved_person_id),
                )
                account_created = bool(cur.fetchone())
    except psycopg2.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
        if constraint in ("uq_tenant_users_owner_person", "uq_user_accounts_owner"):
            print(
                f"error: tenant '{tenant}' already has an owner — only one "
                "person-linked owner is allowed per tenant (migration 039/071)",
                file=sys.stderr,
            )
        elif constraint == "tenant_users_telegram_tenant_key":
            print(
                f"error: telegram id {telegram_id} is already registered in "
                f"tenant '{tenant}'",
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    # ── Memory entity bridge (best-effort; reuses robothor.memory.entities) ──
    memory_note: str | None = None
    entity_id: int | None = None
    try:
        import asyncio

        from robothor.memory.entities import upsert_entity

        entity_id = asyncio.run(upsert_entity(name, "person", tenant_id=tenant))
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE contact_identifiers SET memory_entity_id = %s
                WHERE tenant_id = %s AND person_id = %s AND memory_entity_id IS NULL
                """,
                (entity_id, tenant, resolved_person_id),
            )
    except Exception as exc:  # degrade gracefully -- never block registration
        memory_note = f"memory entity link skipped (notice): {exc}"

    # ── Report what was created/linked ──
    print(f"✓ Person: {resolved_person_id} ({'created' if person_created else 'linked existing'})")
    print(
        f"✓ tenant_users: id={tenant_user_id} user_id={stable_user_id} "
        f"role={role} tenant={tenant}"
    )
    if identifiers_created:
        print(f"✓ contact_identifiers: {', '.join(identifiers_created)}")
    else:
        print("  contact_identifiers: none created (no --telegram-id/--email given, or already mapped)")
    if memory_note:
        print(f"  {memory_note}")
    else:
        print(f"✓ memory entity linked: id={entity_id}")
    if email:
        if account_created:
            print(f"✓ user_accounts invite created for {email} (status=invited)")
        else:
            print(f"  user_accounts: {email} already exists in tenant '{tenant}' (no change)")
        print("Next step — arm the SSO binding grant so this account can sign in:")
        print(f"  robothor auth grant-binding --email {email} --tenant {tenant}")
    return 0


# ─── link ────────────────────────────────────────────────────────────────


def _cmd_link(args: Namespace) -> int:
    import psycopg2

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal as crm_dal
    from robothor.db.connection import get_connection

    tenant = getattr(args, "tenant", None) or DEFAULT_TENANT
    telegram_id = str(args.telegram_id)
    person_id_arg = getattr(args, "person_id", None)
    email = getattr(args, "email", None)

    if person_id_arg:
        person = crm_dal.get_person(person_id_arg, tenant_id=tenant)
        if not person:
            print(f"error: person {person_id_arg} not found in tenant '{tenant}'", file=sys.stderr)
            return 1
        resolved_person_id = person_id_arg
    elif email:
        resolved_person_id = _find_person_by_email(tenant, email)
        if not resolved_person_id:
            print(
                f"error: no person with email {email} in tenant '{tenant}' "
                "(create one first with `robothor user add --create-person`)",
                file=sys.stderr,
            )
            return 1
    else:
        # argparse's mutually-exclusive required group guarantees one of
        # --person-id/--email is set; this is an unreachable defensive guard.
        print("error: one of --person-id or --email is required", file=sys.stderr)
        return 2

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tenant_users SET person_id = %s, updated_at = NOW()
                WHERE telegram_user_id = %s AND tenant_id = %s
                RETURNING id, display_name
                """,
                (resolved_person_id, telegram_id, tenant),
            )
            tu_row = cur.fetchone()
            if not tu_row:
                print(
                    f"error: no tenant_users row for telegram id {telegram_id} in "
                    f"tenant '{tenant}' — register with `robothor user add` first",
                    file=sys.stderr,
                )
                return 1
            _tenant_user_id, display_name = tu_row

            cur.execute(
                """
                INSERT INTO contact_identifiers
                    (tenant_id, channel, identifier, display_name, person_id)
                VALUES (%s, 'telegram', %s, %s, %s)
                ON CONFLICT (tenant_id, channel, identifier) DO UPDATE
                    SET person_id = EXCLUDED.person_id, updated_at = NOW()
                RETURNING id
                """,
                (tenant, telegram_id, display_name, resolved_person_id),
            )
            cur.fetchone()
    except psycopg2.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
        if constraint == "uq_tenant_users_owner_person":
            print(
                f"error: tenant '{tenant}' already has an owner — only one "
                "person-linked owner is allowed per tenant (migration 039)",
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"✓ Linked telegram id {telegram_id} -> person {resolved_person_id} (tenant={tenant})")
    return 0


# ─── link-face ──────────────────────────────────────────────────────────


def _cmd_link_face(args: Namespace) -> int:
    import psycopg2

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal as crm_dal
    from robothor.db.connection import get_connection

    tenant = getattr(args, "tenant", None) or DEFAULT_TENANT
    label = args.label
    person_id = args.person_id

    person = crm_dal.get_person(person_id, tenant_id=tenant)
    if not person:
        print(f"error: person {person_id} not found in tenant '{tenant}'", file=sys.stderr)
        return 1

    missing_table_msg = (
        "error: face_identities not present — run migrations / ships with vision linkage"
    )
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.face_identities')")
            row = cur.fetchone()
            if not row or row[0] is None:
                print(missing_table_msg, file=sys.stderr)
                return 1
            cur.execute(
                """
                INSERT INTO face_identities (tenant_id, face_label, person_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, face_label) DO UPDATE
                    SET person_id = EXCLUDED.person_id
                RETURNING person_id
                """,
                (tenant, label, person_id),
            )
            cur.fetchone()
    except psycopg2.errors.UndefinedTable:
        print(missing_table_msg, file=sys.stderr)
        return 1

    print(f"✓ Linked face label '{label}' -> person {person_id} (tenant={tenant})")
    return 0
