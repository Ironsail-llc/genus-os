"""
Genus OS Bridge Service — Connects agents, CRM, and Memory System.

FastAPI app on port 9100. All CRM operations go through robothor.crm.dal.
Agent RBAC and tenant isolation are derived from verified token claims.
Legacy identity headers require explicit loopback-only insecure development mode.

OpenAPI docs: http://localhost:9100/docs
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager, suppress

# Prevent double-import when run as __main__: ensure 'bridge_service' module
# name resolves to THIS instance so routers see the same http_client.
if __name__ == "__main__":
    sys.modules["bridge_service"] = sys.modules[__name__]

import asyncio
import logging

import httpx
from fastapi import Depends, FastAPI

logger = logging.getLogger(__name__)


def _default_tenant() -> str:
    """Lazy import to avoid circular dependency."""
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT


from middleware import AuthMiddleware, CorrelationMiddleware, RBACMiddleware, TenantMiddleware
from routers.agent_manifests import router as agent_manifests_router
from routers.agents import router as agents_router
from routers.approvals import router as approvals_router
from routers.audit import router as audit_router
from routers.auth import router as auth_router
from routers.auth import sso_secret_present
from routers.automations import router as automations_router
from routers.channel_access import router as channel_access_router
from routers.controls import router as controls_router
from routers.conversations import router as conversations_router
from routers.flag_audit import router as flag_audit_router
from routers.fleet import router as fleet_router
from routers.health import router as health_router
from routers.installed_agents import router as installed_agents_router
from routers.integration import router as integration_router
from routers.logs import router as logs_router
from routers.memory import router as memory_router
from routers.memory_facts import router as memory_facts_router
from routers.notes_tasks import router as notes_tasks_router
from routers.notifications import router as notifications_router
from routers.people import router as people_router
from routers.plugins import router as plugins_router
from routers.providers import router as providers_router
from routers.pursuit_goals import router as pursuit_goals_router
from routers.routines import router as routines_router
from routers.runs import router as runs_router
from routers.settings import router as settings_router
from routers.setup import router as setup_router
from routers.system_health import router as system_health_router
from routers.tenants import router as tenants_router
from routers.users import router as users_router
from routers.workflows import router as workflows_router

from robothor.credential_errors import install_credential_safe_validation

# ─── Configuration ───────────────────────────────────────────────────────

_bridge_config: dict = {
    "memory_url": os.getenv("MEMORY_URL", "http://localhost:9099"),
}

http_client: httpx.AsyncClient | None = None


async def _routine_trigger_loop():
    """Background task: check for due routines every 60s and create tasks."""
    from robothor.crm.dal import advance_routine, create_task, get_due_routines
    from robothor.events.bus import publish

    while True:
        try:
            await asyncio.sleep(60)
            due = get_due_routines()
            for routine in due:
                task_id = create_task(
                    title=routine["title"],
                    body=routine.get("body"),
                    assigned_to_agent=routine.get("assignedToAgent"),
                    priority=routine.get("priority", "normal"),
                    tags=routine.get("tags"),
                    person_id=routine.get("personId"),
                    company_id=routine.get("companyId"),
                    created_by_agent="routine-trigger",
                    tenant_id=routine.get("tenantId", _default_tenant()),
                )
                if task_id:
                    advance_routine(routine["id"])
                    publish(
                        "agent",
                        "routine.triggered",
                        {
                            "routine_id": routine["id"],
                            "task_id": task_id,
                            "title": routine["title"],
                        },
                        source="bridge",
                    )
                    logger.info("Routine '%s' triggered → task %s", routine["title"], task_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Routine trigger loop error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    from robothor.auth.runtime import validate_auth_configuration

    validate_auth_configuration(bind_host=os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1"))
    # Say at boot, once, whether this process can complete a sign-in at all.
    # Outside production the missing secret is not fatal (dev/loopback modes do
    # not use the SSO exchange), so this is a loud log line rather than a raise.
    sso_secret_present()
    http_client = httpx.AsyncClient(timeout=30.0)
    from robothor.autonomy.handoff_worker import purge_expired_observations, recover_checks

    trigger_task = asyncio.create_task(_routine_trigger_loop())
    recovery_task = asyncio.create_task(recover_checks())
    # The observation archive kept the owner's name, date of birth, address
    # and their answers to a website indefinitely. This is what makes
    # `autonomy.terms_retention_days` an actual window rather than a comment.
    purge_task = asyncio.create_task(purge_expired_observations())
    try:
        yield
    finally:
        trigger_task.cancel()
        recovery_task.cancel()
        purge_task.cancel()
        for task in (trigger_task, recovery_task, purge_task):
            with suppress(asyncio.CancelledError):
                await task
        await http_client.aclose()


# ─── App Assembly ────────────────────────────────────────────────────────

app = FastAPI(
    title="Genus OS Bridge",
    version="3.0.0",
    description="Bridge between agents and the Genus OS intelligence layer. Multi-tenant.",
    lifespan=lifespan,
)

# Middleware (applied in reverse order — correlation runs first, then auth,
# then tenant, then RBAC). AuthMiddleware verifies a bridge-issued token and
# sets request.state.auth. Authentication is fail-closed unless the bridge is
# explicitly placed in loopback-only insecure development mode.
app.add_middleware(RBACMiddleware)
app.add_middleware(TenantMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(CorrelationMiddleware)

# Routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(people_router)
app.include_router(providers_router)
app.include_router(conversations_router)
app.include_router(notes_tasks_router)
app.include_router(pursuit_goals_router)
app.include_router(memory_router)
# The operator's view of the memory_facts TABLE (list / forget preview /
# forget). Its own router because its gate is not memory_router's: every route
# is operator-only, where the subsystem proxy above is agent-reachable under
# ``_memory_admin_scope``.
app.include_router(memory_facts_router)
app.include_router(routines_router)
app.include_router(notifications_router)
app.include_router(tenants_router)
app.include_router(integration_router)
app.include_router(installed_agents_router)
# Agent manifests: the Helm's agent builder. Beside installed_agents because
# both write to docs/agents/ — one from the marketplace, one from a form — and
# both have to tell the engine to reconcile afterwards.
app.include_router(agent_manifests_router)
app.include_router(audit_router)
app.include_router(controls_router)
# The guardrail CHANGE LOG, on the same prefix as Controls but with its own
# gate: an auditor may read who flipped what and why, and may reach nothing
# else in controls_router. Separate module so that router's operator-only
# docstring stays true of every route in it.
app.include_router(flag_audit_router)
# Settings: the Config page. Beside Controls because a governed flag is a
# setting whose store happens to be a table -- PATCH /api/settings routes one
# through the same robothor.flags.store call this router's PATCH makes.
app.include_router(settings_router)
app.include_router(fleet_router)
app.include_router(runs_router)
app.include_router(system_health_router)
# journald, for an operator who is not on the box. Beside system_health because
# both answer "what is this appliance doing right now" — one as numbers, one as
# the lines the processes actually printed.
app.include_router(logs_router)
app.include_router(workflows_router)
# Automations: the scheduled half of the fleet, joined to what its last run
# actually did. Beside workflows because the Helm shows them on one screen --
# a cron'd agent and a workflow are the same question to an operator.
app.include_router(automations_router)
# What is waiting on a person, of every kind. Beside workflows because a
# workflow approval is one of the three things it answers; the other two are an
# ask_user question (a row) and a permission escalation (a proxy to the engine,
# where the pending request actually lives).
app.include_router(approvals_router)
from routers.autonomy import router as autonomy_router

# Personal automation, behind the instance-level switch that governs the rest
# of the feature. `require_personal_owner` inside the router checks role and
# identity; it does not ask whether this appliance offers the feature at all,
# so with ROBOTHOR_AUTONOMY_ENABLED off any authenticated member could still
# POST an enrollment or a grant -- the endpoints that store a payment card and
# hand an agent spending authority. Applied here rather than in the router so
# it covers every route including later ones, and so the routes stay in the
# assembled app for test_mutations_are_gated.py to enumerate.
from crm.bridge.autonomy_gate import require_feature_offered

app.include_router(autonomy_router, dependencies=[Depends(require_feature_offered)])
# Who may reach this instance over a channel. Beside approvals because both are
# "a person has to decide something", and one of the decisions here is the only
# way a pairing code is ever spent over the network -- the channel that issued
# it can never spend it.
app.include_router(channel_access_router)
# Installed plugins: the listing, enable/disable, and the reload. Operator-
# gated end to end -- a reload re-imports third-party code into the daemon.
app.include_router(plugins_router)
# Accounts and roles. Operator-gated end to end, the reads included: the listing
# enumerates every account on this appliance and what each of them may do.
app.include_router(users_router)
# First-run setup. Every route here refuses with 404 once an owner account
# exists, so on a claimed appliance this router is mounted and unreachable --
# the gate is asked per request rather than at import, because "has an owner"
# changes while the process runs and an import-time decision would keep the
# wizard alive until the next restart.
app.include_router(setup_router)

# A 422 on the provider/vault routes must not reflect the request body:
# on those routes the body is a credential.
install_credential_safe_validation(app)


def uvicorn_options() -> dict[str, object]:
    """How the bridge is served.

    ``proxy_headers=False`` is deliberate: uvicorn's ProxyHeadersMiddleware
    trusts loopback by default and rewrites ``request.client`` from a
    client-supplied ``X-Forwarded-For``. The bridge has its own explicit
    trusted-proxy logic (``GENUS_TRUSTED_PROXIES``, which trusts nobody by
    default); two mechanisms that disagree would let a caller choose the peer
    address the rate limiter and the audit trail key on.
    """
    return {
        "host": os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ROBOTHOR_BRIDGE_PORT", "9100")),
        "proxy_headers": False,
    }


if __name__ == "__main__":
    import uvicorn

    from robothor.engine.process_hardening import harden_process

    # Same-uid processes share procfs: an agent's `exec` child can read this
    # process's environment out of /proc unless it says otherwise. One call,
    # from the one helper, in every long-running Genus process — hardening the
    # engine alone was a statistic, not a boundary.
    harden_process()
    uvicorn.run(app, **uvicorn_options())  # type: ignore[arg-type]
