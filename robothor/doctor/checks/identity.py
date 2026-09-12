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
    providers page nobody can configure. This is NOT repaired automatically:
    creating a privileged account is an act an operator performs deliberately,
    and a doctor that minted one on its own would be a privilege-escalation
    path that runs from cron.
    """
    tenant = ""

    def _probe() -> tuple[str, int]:
        from robothor.constants import DEFAULT_TENANT
        from robothor.owner_config import load_owner_config

        owner = load_owner_config()
        tenant_id = (owner.tenant_id if owner else "") or DEFAULT_TENANT
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM user_accounts "
                "WHERE tenant_id = %s AND role = 'owner' AND status = 'active'",
                (tenant_id,),
            )
            row = cursor.fetchone()
        count = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        return tenant_id, int(count)

    try:
        tenant, count = await ctx.run_blocking(_probe)
    except Exception as exc:  # noqa: BLE001 - no table, no database: both are answers
        return fail(f"cannot read user_accounts: {type(exc).__name__}")
    if count == 0:
        return fail(
            f"no active owner account for tenant {tenant} — nobody can sign in; "
            "create one with 'genus user add --role owner'"
        )
    return ok(f"{count} active owner account(s) for tenant {tenant}")


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
