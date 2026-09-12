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

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: How many tenants the mismatch line names before it summarises. The manifest
#: checks cap at the same number and for the same reason: this detail reaches a
#: terminal table and the bridge's JSON, and a many-tenant instance would
#: otherwise put kilobytes into both.
_MAX_LISTED = 8


def _summarise_tenants(counts: dict[str, int]) -> str:
    shown = sorted(counts.items())
    head = ", ".join(f"{name} ({n})" for name, n in shown[:_MAX_LISTED])
    if len(shown) > _MAX_LISTED:
        head += f", and {len(shown) - _MAX_LISTED} more"
    return head


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


def _first_run_is_pending(ctx: DoctorContext) -> bool:
    """Has this instance been installed but never claimed in the browser?

    ``genus init`` deliberately creates no account: the first-run wizard does,
    with the operator's password. So "there is no owner yet" is the EXPECTED
    state of a correct fresh install, and the signal that the ceremony has not
    happened is the marker the wizard itself writes when it finishes --
    ``setup_completed_at`` in the workspace's ``config.yaml``.

    The wizard's own marker rather than the setup token, because the token has
    a lifetime and an ordering: ``genus init`` mints it in its LAST step, after
    the verification that asks this question, and it expires in half an hour.
    A doctor run a day later would then call a never-claimed instance broken.

    Unreadable reads as NOT pending, which is the conservative direction: the
    check stays required, and a genuinely ownerless instance is still reported.
    """
    try:
        from robothor import setup_token

        return not setup_token.setup_recorded(ctx.workspace)
    except Exception:  # noqa: BLE001 - unknown is not "still installing"
        return False


async def _owner_account(ctx: DoctorContext) -> Result:
    """An ``owner`` user account exists, so somebody can sign in to the Helm.

    The bridge admits only ``owner`` and ``admin`` to every operator surface.
    An instance with no owner row has a dashboard nobody can enter and a
    providers page nobody can configure.

    Counted across every tenant the connection can SEE, not only the one
    ``owner.yaml`` names, because those two disagreeing is a different fault
    with a different repair -- and the first Genus OS instance had exactly that.
    ``owner.yaml`` said one tenant, the live owner accounts were under another,
    people signed in every day, and a doctor that asked only about the owner
    file's tenant reported "nobody can sign in" and told the operator to run
    ``genus user add --role owner``. Following that would have minted a SECOND
    privileged account, in the wrong tenant, on a healthy instance.

    "Every tenant the connection can see" is the load-bearing qualification.
    Migration 081 enables and FORCEs row-level security on every table carrying
    a ``tenant_id``, ``user_accounts`` included, with a policy that filters to
    ``app.tenant_id`` whenever it is set -- and ``_apply_tenant_scope`` binds it
    on every checkout once RLS is on, with the services connecting as the
    non-superuser role migration 082 exists for. On that posture, which is the
    one the runbook prescribes, an unqualified GROUP BY returns ONE tenant's
    rows. So the scope is read in the same transaction, and a read that was
    scoped says so instead of claiming "no owner in any tenant" -- a claim it
    cannot support, and one that used to carry the prescription this check
    deleted. A scoped read never prescribes ``genus user add``: it does not know
    whether an owner exists somewhere it was not allowed to look.

    Tenant ids are configuration, not credentials, so they are safe to print --
    and they are the whole content of the finding: without them the operator
    cannot tell which side to change. The enumeration is capped, because this
    detail reaches an operator's terminal and the bridge's JSON.

    Never repaired automatically: creating a privileged account is an act an
    operator performs deliberately, and a doctor that minted one on its own
    would be a privilege-escalation path that runs from cron.
    """

    def _probe() -> tuple[str, str, dict[str, int]]:
        from robothor.constants import DEFAULT_TENANT
        from robothor.owner_config import load_owner_config

        owner = load_owner_config()
        tenant_id = (owner.tenant_id if owner else "") or DEFAULT_TENANT
        with ctx.db() as conn:
            cursor = conn.cursor()
            # Same transaction as the count, so the scope reported is the scope
            # the count was taken under. `true` for missing_ok: the setting is
            # absent entirely when RLS is off, and current_setting would
            # otherwise raise rather than answer.
            cursor.execute("SELECT current_setting('app.tenant_id', true)")
            scope_row = cursor.fetchone()
            cursor.execute(
                "SELECT tenant_id, COUNT(*) FROM user_accounts "
                "WHERE role = 'owner' AND status = 'active' GROUP BY tenant_id"
            )
            rows = cursor.fetchall()
        scope = str(scope_row[0]) if scope_row and scope_row[0] else ""
        counts = {str(row[0]): int(row[1]) for row in rows}
        return tenant_id, scope, counts

    try:
        tenant, scope, counts = await ctx.run_blocking(_probe)
    except Exception as exc:  # noqa: BLE001 - no table, no database: both are answers
        return fail(f"cannot read user_accounts: {type(exc).__name__}")

    here = counts.get(tenant, 0)
    if here:
        return ok(f"{here} active owner account(s) for tenant {tenant}")

    elsewhere = {name: n for name, n in counts.items() if n}
    if elsewhere:
        return fail(
            f"owner.yaml names tenant {tenant}, but every active owner account visible "
            f"here is under {_summarise_tenants(elsewhere)} — reconcile "
            "~/.robothor/owner.yaml with the tenant this instance is configured for "
            "(ROBOTHOR_DEFAULT_TENANT, or ROBOTHOR_TENANT_ID / "
            "ROBOTHOR_PLATFORM_TENANT if either is set). The accounts are fine; do NOT "
            "create another owner"
        )

    if scope:
        # A scoped read cannot see another tenant's rows, so "there is no owner"
        # is not a conclusion available to it.
        return fail(
            f"no active owner account visible in RLS scope {scope} (owner.yaml names "
            f"{tenant}) — this connection is tenant-scoped, so it cannot tell whether an "
            "owner exists in another tenant; check from an unscoped session, or reconcile "
            "owner.yaml with the scope"
        )

    if _first_run_is_pending(ctx):
        # Not a fault: `genus init` hands the browser wizard the job of creating
        # the operator, precisely so the account arrives WITH a password. Telling
        # the operator to mint one by hand here would close the wizard they have
        # not opened yet -- the gate 404s every first-run route the moment any
        # owner row exists.
        return skip(
            f"no owner account yet (owner.yaml names {tenant}) — finish the first run at "
            "/setup using the link `genus init` printed, or run `genus auth setup-link` "
            "for a fresh one"
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
