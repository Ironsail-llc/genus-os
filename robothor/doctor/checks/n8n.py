"""n8n monitor checks: configured, reachable, polling.

All three skip unless ``ROBOTHOR_N8N_ENABLED`` is on -- the monitor is opt-in,
and an instance without n8n is not missing anything.

* ``n8n.configured`` (recommended) -- a base URL and an API key are set.
* ``n8n.connection`` (recommended) -- online only: ``/healthz`` answers and
  the key can list a workflow. Skipped under ``--offline``.
* ``n8n.poll_freshness`` (recommended) -- the poller last succeeded within
  three poll intervals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext
    from robothor.integrations.n8n.client import N8nClient
    from robothor.settings.model import GenusSettings

__all__ = ["CHECKS"]

_DISABLED = "the n8n monitor is off (ROBOTHOR_N8N_ENABLED is not set)"
#: How many poll intervals may pass before the poller counts as stalled.
FRESHNESS_INTERVALS = 3


def _tenant(ctx: DoctorContext) -> str:
    from robothor.constants import DEFAULT_TENANT

    return ctx.settings.database.tenant_id or DEFAULT_TENANT


def _key_source(tenant_id: str) -> str:
    """Where the API key resolves from: env, vault, missing or unavailable. Blocking."""
    from robothor.integrations.n8n.poller import API_KEY_ENV
    from robothor.secrets import secret_source

    return str(secret_source(API_KEY_ENV, tenant_id=tenant_id))


def _make_client(settings: GenusSettings, tenant_id: str) -> N8nClient:
    """A client for ``tenant_id``'s key. Blocking (the vault read)."""
    from robothor.integrations.n8n.poller import client_from_settings

    return client_from_settings(settings, tenant_id=tenant_id)


# ── n8n.configured ───────────────────────────────────────────────────────────


async def _configured(ctx: DoctorContext) -> Result:
    """The n8n monitor knows where n8n is and holds a key for it.

    Fails when ``ROBOTHOR_N8N_BASE_URL`` is empty or not an ``https://`` URL
    (``http://`` is accepted for localhost, or anywhere with
    ``ROBOTHOR_N8N_ALLOW_INSECURE_HTTP`` for an n8n on a private network), or no
    ``ROBOTHOR_N8N_API_KEY`` resolves (vault first, then the environment):
    the poller then fails every round and the dashboard shows stale data.
    Fix: set the base URL, then store the key in the vault (``genus vault set
    providers/robothor_n8n/api_key``) or in the environment file.
    """
    group = ctx.settings.integrations
    if not group.n8n_enabled:
        return skip(_DISABLED)
    base_url = (group.n8n_base_url or "").strip()
    if not base_url:
        return fail("ROBOTHOR_N8N_BASE_URL is empty: set it to the n8n instance's root URL")
    from robothor.integrations.n8n.client import N8nError, checked_root

    try:
        checked_root(base_url, allow_insecure_http=bool(group.n8n_allow_insecure_http))
    except N8nError as exc:
        return fail(str(exc).removeprefix("not_configured: "))
    source = await ctx.run_blocking(_key_source, _tenant(ctx))
    if source in ("env", "vault"):
        return ok(f"n8n at {base_url}; API key from the {source}")
    if (group.n8n_api_key or "").strip():
        return ok(f"n8n at {base_url}; API key from config.yaml")
    if source == "unavailable":
        return fail("the vault could not be read, so ROBOTHOR_N8N_API_KEY cannot be confirmed")
    return fail("no ROBOTHOR_N8N_API_KEY is stored (vault or environment)")


# ── n8n.connection ───────────────────────────────────────────────────────────


async def _connection(ctx: DoctorContext) -> Result:
    """n8n answers, and the API key can read its workflows.

    Calls ``/healthz`` and ``GET /api/v1/workflows?limit=1``. A failure names
    the class only -- ``auth`` (wrong or revoked key: create a new one in
    n8n's Settings > n8n API), ``network`` (unreachable or 5xx), ``rate_limit``
    or ``not_configured``. Skipped under ``--offline``.
    """
    if not ctx.settings.integrations.n8n_enabled:
        return skip(_DISABLED)
    if ctx.offline:
        return skip("--offline: n8n is not called")
    from robothor.integrations.n8n.client import N8nError

    try:
        client = await ctx.run_blocking(_make_client, ctx.settings, _tenant(ctx))
        async with client:
            await client.healthz()
            count = await client.count_workflows(limit=1)
    except N8nError as exc:
        return fail(f"n8n call failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - a check reports, it does not raise
        return fail(f"n8n call failed: {type(exc).__name__}")
    return ok(f"n8n is up and the key can list workflows ({count} on the first page)")


# ── n8n.poll_freshness ───────────────────────────────────────────────────────


def _poll_age(ctx: DoctorContext, tenant_id: str) -> tuple[float | None, str | None] | None:
    """(seconds since the last good poll, last error), or None with no row. Blocking."""
    with ctx.db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - last_ok_at)), last_error "
            "FROM n8n_sync_state WHERE tenant_id = %s",
            (tenant_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return (None if row[0] is None else float(row[0])), row[1]


async def _poll_freshness(ctx: DoctorContext) -> Result:
    """The n8n poller completed a round recently.

    Fresh means within three ``ROBOTHOR_N8N_POLL_SECONDS``. A poller that
    never succeeded, or stopped, fails: n8n failures are then not reaching
    the main agent and the dashboard is stale. Fix: run ``n8n.connection``,
    and check the engine journal for ``n8n poll deferred``.
    """
    group = ctx.settings.integrations
    if not group.n8n_enabled:
        return skip(_DISABLED)
    from robothor.integrations.n8n.contract import effective_poll_seconds

    limit = FRESHNESS_INTERVALS * effective_poll_seconds(group.n8n_poll_seconds)
    try:
        found = await ctx.run_blocking(_poll_age, ctx, _tenant(ctx))
    except Exception as exc:  # noqa: BLE001 - a check reports, it does not raise
        return fail(
            f"could not read the n8n sync state ({type(exc).__name__}); is migration 156 applied?"
        )
    age, error = found if found is not None else (None, None)
    suffix = f" (last error: {error})" if error else ""
    if age is None:
        return fail("the n8n poller has never completed a round" + suffix)
    if age > limit:
        return fail(
            f"the n8n poller last succeeded {int(age)}s ago (allowed {limit}s = "
            f"{FRESHNESS_INTERVALS} x the poll interval){suffix}"
        )
    return ok(f"the n8n poller last succeeded {int(age)}s ago")


CHECKS: tuple[Check, ...] = (
    Check(
        id="n8n.configured",
        title="n8n monitor: base URL and API key are set",
        category="integrations",
        severity="recommended",
        run=_configured,
    ),
    Check(
        id="n8n.connection",
        title="n8n monitor: n8n answers and the key can list workflows",
        category="integrations",
        severity="recommended",
        run=_connection,
    ),
    Check(
        id="n8n.poll_freshness",
        title="n8n monitor: the poller is keeping up",
        category="integrations",
        severity="recommended",
        run=_poll_freshness,
    ),
)
