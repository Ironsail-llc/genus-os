"""Who owns this instance?

Two different questions with two different failure modes. ``owner.yaml`` is
what the ENGINE reads to know whose assistant it is -- without it agents
address "there" and every "who am I talking to" resolution falls back. The
owner ACCOUNT is what the bridge checks before letting anyone into the Helm --
without it, nobody can sign in at all, and the instance is running with no way
to operate it.

Neither check prints an email address. The operator's identity is instance
data; a platform diagnostic that echoed it into a log or a dashboard would put
it somewhere it does not belong (CLAUDE.md rule 1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


async def _owner_config(ctx: DoctorContext) -> Result:
    """``~/.robothor/owner.yaml`` exists and names an operator with an email.

    Without it the engine has no operator identity: ``get_owner_person`` cannot
    resolve the CRM row, delivery has nobody to address, and the owner account
    below cannot be seeded because there is nothing to seed it from. Copy
    ``templates/owner.yaml.example`` and fill it in, or run ``genus init``.
    """

    def _load() -> tuple[bool, bool, str]:
        from robothor.owner_config import load_owner_config

        owner = load_owner_config()
        if owner is None:
            return False, False, ""
        return True, bool(owner.email), owner.tenant_id or ""

    found, has_email, tenant = await ctx.run_blocking(_load)
    if not found:
        return fail(
            "no operator configured — write ~/.robothor/owner.yaml "
            "(copy templates/owner.yaml.example) or run 'genus init'"
        )
    if not has_email:
        return fail("owner.yaml is present but carries no email address")
    return ok(f"operator configured{f' for tenant {tenant}' if tenant else ''}")


async def _owner_account(ctx: DoctorContext) -> Result:
    """An ``owner`` user account exists, so somebody can sign in to the Helm.

    The bridge admits only ``owner`` and ``admin`` to every operator surface.
    An instance with no owner row has a dashboard nobody can enter and a
    providers page nobody can configure.

    Counted across EVERY tenant, not only the one ``owner.yaml`` names, because
    those two disagreeing is a different fault with a different repair -- and
    the first Genus OS instance had exactly that. ``owner.yaml`` said one
    tenant, the live owner accounts were under another, people signed in every
    day, and a doctor that asked only about the owner file's tenant reported
    "nobody can sign in" and told the operator to run ``genus user add --role
    owner``. Following that would have minted a SECOND privileged account, in
    the wrong tenant, on a healthy instance. So a mismatch is reported as a
    mismatch, names both tenant ids, and prescribes reconciling the file.

    Tenant ids are configuration, not credentials, so they are safe to print --
    and they are the whole content of the finding: without both of them the
    operator cannot tell which side to change.

    Never repaired automatically: creating a privileged account is an act an
    operator performs deliberately, and a doctor that minted one on its own
    would be a privilege-escalation path that runs from cron.
    """

    def _probe() -> tuple[str, dict[str, int]]:
        from robothor.constants import DEFAULT_TENANT
        from robothor.owner_config import load_owner_config

        owner = load_owner_config()
        tenant_id = (owner.tenant_id if owner else "") or DEFAULT_TENANT
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tenant_id, COUNT(*) FROM user_accounts "
                "WHERE role = 'owner' AND status = 'active' GROUP BY tenant_id"
            )
            rows = cursor.fetchall()
        counts = {str(row[0]): int(row[1]) for row in rows}
        return tenant_id, counts

    try:
        tenant, counts = await ctx.run_blocking(_probe)
    except Exception as exc:  # noqa: BLE001 - no table, no database: both are answers
        return fail(f"cannot read user_accounts: {type(exc).__name__}")

    here = counts.get(tenant, 0)
    if here:
        return ok(f"{here} active owner account(s) for tenant {tenant}")

    elsewhere = {name: n for name, n in counts.items() if n}
    if elsewhere:
        where = ", ".join(f"{name} ({n})" for name, n in sorted(elsewhere.items()))
        return fail(
            f"owner.yaml names tenant {tenant}, but every active owner account is under "
            f"{where} — reconcile ~/.robothor/owner.yaml with ROBOTHOR_DEFAULT_TENANT "
            "(the accounts are fine; do NOT create another owner)"
        )

    return fail(
        f"no active owner account in any tenant (owner.yaml names {tenant}) — nobody can "
        "sign in; create one with 'genus user add --role owner'"
    )


CHECKS: tuple[Check, ...] = (
    Check(
        id="identity.owner_config",
        title="An operator is configured",
        category="identity",
        severity="required",
        run=_owner_config,
    ),
    Check(
        id="identity.owner_account",
        title="An owner account exists",
        category="identity",
        severity="required",
        run=_owner_account,
    ),
)
