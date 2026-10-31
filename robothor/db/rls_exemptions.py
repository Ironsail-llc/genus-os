"""Tenant tables deliberately WITHOUT row-level security.

The platform invariant is that every table with a ``tenant_id`` column carries
the ``tenant_isolation`` policy (migrations 081, 120, 132). Three places enforce
it: the static migration guard (``tests/test_tenant_rls_coverage.py``), the
doctor's ``db.rls_coverage`` check, and that check's ``--fix`` backstop. A table
listed here is exempt from all three, and each entry says why.

Keep this list short. An exemption is a table whose tenant scoping is done by
explicit predicates in code, because RLS would hide rows from a reader who is
meant to see every tenant's.
"""

from __future__ import annotations

#: table -> why it carries ``tenant_id`` but no ``tenant_isolation`` policy.
RLS_EXEMPT_TENANT_TABLES: dict[str, str] = {
    "audit_log": (
        "The appliance-wide audit trail. The platform's root operator or auditor "
        "at home reads every tenant's rows and the untenanted rows written before "
        "migration 162; everyone else is scoped by explicit predicates in "
        "routers._operator.audit_scope. With RLS on it, the bridge's per-request "
        "tenant binding would hide exactly the rows the root reader is meant to see."
    ),
}


def sql_literal_list() -> str:
    """The exempt table names as a SQL list literal, e.g. ``('audit_log')``.

    Safe to inline: the names are constants in this module, checked to be
    plain identifiers by its tests, never caller input.
    """
    return "(" + ", ".join(f"'{name}'" for name in sorted(RLS_EXEMPT_TENANT_TABLES)) + ")"
