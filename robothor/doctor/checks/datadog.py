"""Datadog read access: ``datadog.reachable`` (recommended).

Skips unless the Datadog keys are set -- an instance that does not read
Datadog is not missing anything. With keys, it asks Datadog to validate the
API key, and names the active tenants that have no scope entry (their reads
are refused until the operator adds one).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext
    from robothor.integrations.datadog.client import DatadogClient
    from robothor.settings.model import GenusSettings

__all__ = ["CHECKS"]


def _keys_set(settings: GenusSettings) -> bool:
    """Both keys resolve (vault first, then the environment). Blocking."""
    from robothor.integrations.datadog.client import resolve_keys

    api_key, app_key = resolve_keys(settings)
    return bool(api_key and app_key)


def _make_client(settings: GenusSettings) -> DatadogClient:
    """The configured client. Blocking (the vault read)."""
    from robothor.integrations.datadog.client import client_from_settings

    return client_from_settings(settings)


def _unscoped_tenants(ctx: DoctorContext) -> list[str]:
    """Active tenants with no usable scope entry, less the benchmark sandbox. Blocking.

    The sandbox tenant only runs benchmark sub-runs; it never needs Datadog,
    so listing it would be noise on every install.
    """
    from robothor.engine.benchmark_sandbox import sandbox_tenant_id
    from robothor.integrations.datadog.scope import scope_for

    with ctx.db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM crm_tenants WHERE active = TRUE ORDER BY id")
        tenants = [str(row[0]) for row in cur.fetchall()]
    sandbox = sandbox_tenant_id()
    return [t for t in tenants if t != sandbox and scope_for(t) is None]


async def _reachable(ctx: DoctorContext) -> Result:
    """The shared Datadog keys are accepted, and which tenants have no scope.

    Skips when ``ROBOTHOR_DATADOG_API_KEY`` or ``ROBOTHOR_DATADOG_APP_KEY`` is
    unset, and under ``--offline``. Fails when ``ROBOTHOR_DATADOG_SITE`` is not
    a known Datadog site, when Datadog rejects the API key (create a new key
    on a read-only service account and store it with ``genus vault set``), or
    by kind when Datadog cannot be reached. Active tenants without an entry
    in ``ROBOTHOR_DATADOG_SCOPES`` are named in the detail: their reads are
    refused until one is added.
    """
    from robothor.integrations.datadog.client import DatadogError

    if not await ctx.run_blocking(_keys_set, ctx.settings):
        return skip(
            "Datadog is not configured (ROBOTHOR_DATADOG_API_KEY and "
            "ROBOTHOR_DATADOG_APP_KEY are not both set)"
        )
    if ctx.offline:
        return skip("--offline: Datadog is not called")
    try:
        client = await ctx.run_blocking(_make_client, ctx.settings)
        await ctx.run_blocking(client.validate)
    except DatadogError as exc:
        if exc.kind == "not_configured":
            return fail("ROBOTHOR_DATADOG_SITE is not a known Datadog site; keys were not sent")
        if exc.kind == "auth":
            return fail("Datadog rejected the API key: store a valid read-only key")
        return fail(f"Datadog call failed: {exc.kind}")
    except Exception as exc:  # noqa: BLE001 - a check reports, it does not raise
        return fail(f"Datadog call failed: {type(exc).__name__}")
    # /api/v1/validate ignores the application key, so a second call that needs
    # it (and log read access) proves the pair; its filter matches no line.
    try:
        await ctx.run_blocking(client.validate_app_key)
    except DatadogError as exc:
        if exc.kind == "auth":
            return fail(
                "Datadog rejected the application key, or it may not read logs: "
                "store a read-only key with log read access"
            )
        return fail(f"Datadog call failed: {exc.kind}")
    except Exception as exc:  # noqa: BLE001 - a check reports, it does not raise
        return fail(f"Datadog call failed: {type(exc).__name__}")
    detail = f"Datadog accepts both keys ({client.site})"
    try:
        unscoped = await ctx.run_blocking(_unscoped_tenants, ctx)
    except Exception as exc:  # noqa: BLE001 - the tenant list is a note, not the verdict
        return ok(f"{detail}; could not list tenants ({type(exc).__name__})")
    if unscoped:
        # A fact, not a fault: an install need not give every tenant Datadog.
        detail += (
            f"; {', '.join(unscoped)} have no scope in ROBOTHOR_DATADOG_SCOPES and "
            "are refused Datadog reads until a scope is added for them"
        )
    return ok(detail)


CHECKS: tuple[Check, ...] = (
    Check(
        id="datadog.reachable",
        title="Datadog: the shared read-only keys are accepted",
        category="integrations",
        severity="recommended",
        run=_reachable,
    ),
)
