"""Move a workload's history from one tenant to another.

A *workload* is a set of agents and workflows. When an operator moves one from
the tenant it used to run in (typically the root) to another tenant's engine
(typically a child), its history has to follow: the runs and their steps, the
schedules, the workflow runs, the chat sessions. ``genus tenant move-workload``
does that in ONE transaction, after every refusal has been checked.

Every tenant-bearing table in the schema is classified here, exactly once:

- :data:`MOVED_TABLES` — rows of the workload, found by a predicate on the
  agents' ids, the workflows' ids, or the ids of the runs and sessions those
  select. Re-tenanted in place.
- :data:`UNTOUCHED_TABLES` — rows that belong to the tenant, not to any one
  workload (CRM, memory, identity, credentials, the audit trail). Each entry
  says why.
- :data:`REFUSED_IF_PRESENT` — rows that tie the workload to something that
  cannot move with it. If the workload has any, the move is refused.

Tables with no ``tenant_id`` that hang off a moved table by foreign key (run
steps, chat messages) follow their parent with no write at all:
:data:`FOLLOWS_PARENT` names them so the report can count them.

The move reads the catalog first and refuses if any tenant-bearing table is
not classified, so a migration that adds one cannot be skipped silently (the
integration tests red on it too).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from typing import Any

import psycopg2.extensions
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from robothor.db.connection import get_connection, read_every_tenant_in_transaction

logger = logging.getLogger(__name__)

__all__ = [
    "FOLLOWS_PARENT",
    "MOVED_TABLES",
    "REFUSED_IF_PRESENT",
    "UNTOUCHED_TABLES",
    "WorkloadMoveError",
    "WorkloadMoveReport",
    "move_workload",
]

# The predicates below run inside ``WHERE tenant_id = <source> AND (...)``.
# They read three temporary tables the move fills first (see ``_select``):
_RUNS = "(SELECT id FROM _mw_runs)"
_RUN_TEXT = "(SELECT id::text FROM _mw_runs)"
_WF_RUNS = "(SELECT id FROM _mw_wf_runs)"
_SESSION_KEYS = "(SELECT session_key FROM _mw_sessions)"
_AGENT = "agent_id = ANY(%(agents)s::text[])"

#: table -> the rows of the workload in it. In the order they are rewritten.
MOVED_TABLES: dict[str, str] = {
    "agent_runs": f"id IN {_RUNS}",
    "agent_runtime_controls": f"run_id IN {_RUNS}",
    "agent_runtime_effects": f"run_id IN {_RUN_TEXT}",
    "agent_breadcrumbs": f"{_AGENT} OR run_id IN {_RUN_TEXT}",
    "agent_questions": f"{_AGENT} OR run_id IN {_RUNS}",
    "agent_reviews": f"{_AGENT} OR run_id IN {_RUNS}",
    "run_interventions": f"run_id IN {_RUNS}",
    "agent_schedules": _AGENT,
    "user_cronjobs": _AGENT,
    "benchmark_results": _AGENT,
    "ma_runs": _AGENT,
    "coding_jobs": f"run_id IN {_RUN_TEXT}",
    "fact_access_log": f"run_id IN {_RUN_TEXT}",
    "memory_write_jobs": f"run_id IN {_RUN_TEXT}",
    "message_reactions": f"run_id IN {_RUNS}",
    "workflow_runs": f"id IN {_WF_RUNS}",
    "workflow_approvals": f"run_id IN {_WF_RUNS}",
    "chat_sessions": "id IN (SELECT id FROM _mw_sessions)",
    "chat_approval_receipts": f"session_key IN {_SESSION_KEYS}",
    "channel_message_map": f"session_key IN {_SESSION_KEYS} OR author_run_id IN {_RUNS}",
}

#: Untenanted child table -> (the moved table it hangs off, its key column).
FOLLOWS_PARENT: dict[str, tuple[str, str]] = {
    "agent_run_steps": ("agent_runs", "run_id"),
    "agent_run_checkpoints": ("agent_runs", "run_id"),
    "agent_run_evidence": ("agent_runs", "run_id"),
    "agent_guardrail_events": ("agent_runs", "run_id"),
    "agent_tool_events": ("agent_runs", "run_id"),
    "workflow_run_steps": ("workflow_runs", "run_id"),
    "chat_messages": ("chat_sessions", "session_id"),
}

#: table -> (the workload's rows in it, why their presence refuses the move).
REFUSED_IF_PRESENT: dict[str, tuple[str, str]] = {
    "pursuit_goal_attempts": (
        f"run_id IN {_RUNS}",
        "a goal pursuit spans runs of several agents and links CRM tasks that stay "
        "in the source tenant; complete or cancel the goal first",
    ),
    "autonomy_operations": (
        f"{_AGENT} AND state IN ('reserved', 'submitting', 'reconciling', 'awaiting_input')",
        "an open autonomous operation spends under a grant held in the source tenant; "
        "let it settle or cancel it first",
    ),
    "autonomy_workflows": (
        f"{_AGENT} AND state = 'open'",
        "an open autonomy workflow belongs to a grant held in the source tenant; close it first",
    ),
    "autonomy_handoffs": (
        f"{_AGENT} AND state IN ('awaiting_external_action', 'checking')",
        "a pending autonomy handoff is resolved by the source tenant's engine; "
        "resolve or expire it first",
    ),
}

_CRM = "a CRM record of the tenant, not of one workload; it stays with the tenant's CRM"
_MEMORY = (
    "tenant memory shared by every agent of the tenant; it has no per-agent owner "
    "(an authoring agent id is attribution, not ownership)"
)
_IDENTITY = "identity and access, moved per account by `genus tenant move-account`"
_SECRETS = "the tenant's credentials; a workload never carries secrets to another tenant"
_WORKSPACE = "a mirror of the tenant's connected workspace accounts, synced per tenant"
_AUTONOMY = (
    "the autonomy ledger of a grant held in the source tenant; it stays with the grant "
    "(an open operation, workflow or handoff of the workload refuses the move instead)"
)
_PURSUIT = (
    "goal-pursuit state; a pursuit spans runs and CRM tasks, and a pursuit with an "
    "attempt on a workload run refuses the move (pursuit_goal_attempts)"
)
_OPERATIONS = "the tenant's durable-operations ledger, keyed by job, not by agent or workflow"
_SALES = "the tenant's sales pipeline, keyed by prospect and request, not by agent"

#: table -> why a workload's move leaves its rows where they are.
UNTOUCHED_TABLES: dict[str, str] = {
    "audit_log": "the audit trail keeps the tenant each row was written under (RLS-exempt)",
    "agent_memory_blocks": _MEMORY,
    "agent_messages": "inter-agent mail between two agents, one of which may stay behind",
    "agent_runtime_request_stops": "a short-lived stop flag for one chat request, not a run",
    "autonomy_enrollments": _AUTONOMY,
    "autonomy_events": _AUTONOMY,
    "autonomy_grants": _AUTONOMY,
    "autonomy_payment_events": _AUTONOMY,
    "autonomy_settings": _AUTONOMY,
    "autonomy_terms_snapshots": _AUTONOMY,
    "autonomy_workflow_commands": _AUTONOMY,
    "calendar_event": _WORKSPACE,
    "calendar_event_participant": _WORKSPACE,
    "calendar_operations": (
        "a write against the source tenant's connected calendar, resolved by the "
        "connector that owns that calendar"
    ),
    "call_log": "the tenant's telephony record, keyed by call, not by agent",
    "channel_conversation_refs": "routing for the tenant's own channel connector",
    "channel_pairing_codes": "pairing codes for the tenant's own channel connector",
    "connected_account": _WORKSPACE,
    "contact_identifiers": _CRM,
    "crm_agent_notifications": _CRM,
    "crm_companies": _CRM,
    "crm_conversations": _CRM,
    "crm_curator_state": "one row per tenant: the CRM curator's own cursor",
    "crm_messages": _CRM,
    "crm_notes": _CRM,
    "crm_people": _CRM,
    "crm_routines": _CRM,
    "crm_task_history": _CRM,
    "crm_tasks": _CRM,
    "devops_metrics_snapshots": "tenant-wide metrics snapshots, not one workload's",
    "face_identities": _IDENTITY,
    "fact_access_rollup": "an aggregate over the tenant's memory facts, which stay",
    "federation_connections": "a federation link between tenants, not a workload's",
    "federation_events": "a federation link between tenants, not a workload's",
    "goal_pursuit_settings": _PURSUIT,
    "ingested_items": "ingestion state of the tenant's own sources",
    "ingestion_watermarks": "ingestion state of the tenant's own sources",
    "kb_chunks": "the tenant's knowledge base, shared by every agent",
    "kb_documents": "the tenant's knowledge base, shared by every agent",
    "ma_tenant_resources": "managed-agent provider resources provisioned per tenant",
    "memory_block_prune_log": _MEMORY,
    "memory_conflict_decisions": _MEMORY,
    "memory_entities": _MEMORY,
    "memory_episodes": _MEMORY,
    "memory_facts": _MEMORY,
    "memory_facts_audit": _MEMORY,
    "memory_insights": _MEMORY,
    "memory_intents": _MEMORY,
    "memory_procedures": _MEMORY,
    "memory_relations": _MEMORY,
    "memory_vault": _SECRETS,
    "message": _WORKSPACE,
    "message_participant": _WORKSPACE,
    "message_thread": _WORKSPACE,
    "n8n_executions": "the n8n mirror, keyed by the n8n instance's own workflow ids",
    "n8n_sync_state": "the n8n mirror, keyed by the n8n instance's own workflow ids",
    "n8n_workflows": "the n8n mirror, keyed by the n8n instance's own workflow ids",
    "operation_actions": _OPERATIONS,
    "operation_audit": _OPERATIONS,
    "operation_budgets": _OPERATIONS,
    "operation_effects": _OPERATIONS,
    "operation_fragments": _OPERATIONS,
    "operation_inbox": _OPERATIONS,
    "operation_jobs": _OPERATIONS,
    "operation_reservations": _OPERATIONS,
    "password_reset_tokens": _IDENTITY,
    "plugin_state": "plugin state of the tenant, keyed by plugin, not by agent",
    "tenant_plugin_grants": (
        "a tenant's plugin grants belong to the tenant, set by its root; "
        "they do not move with a workload"
    ),
    "pr_review_cursors": "PR review state of the tenant's repositories",
    "pr_review_messages": "PR review state of the tenant's repositories",
    "pr_reviews": "PR review state of the tenant's repositories",
    "pursuit_goal_events": _PURSUIT,
    "pursuit_goal_history": _PURSUIT,
    "pursuit_goal_provider_reservations": _PURSUIT,
    "pursuit_goal_tasks": _PURSUIT,
    "pursuit_goals": _PURSUIT,
    "role_permissions": _IDENTITY,
    "sales_business_observation_history": _SALES,
    "sales_business_observations": _SALES,
    "sales_calibration_assessments": _SALES,
    "sales_calibration_cohorts": _SALES,
    "sales_calibration_items": _SALES,
    "sales_contacts": _SALES,
    "sales_customer_bindings": _SALES,
    "sales_deployments": _SALES,
    "sales_dossier_history": _SALES,
    "sales_messages": _SALES,
    "sales_outcomes": _SALES,
    "sales_policies": _SALES,
    "sales_prospects": _SALES,
    "sales_request_members": _SALES,
    "sales_requests": _SALES,
    "sales_send_slots": _SALES,
    "sales_settings": _SALES,
    "sales_suppression": _SALES,
    "sso_binding_grants": _IDENTITY,
    "tenant_users": _IDENTITY,
    "timeline_activity": _CRM,
    "user_accounts": _IDENTITY,
    "user_channel_identities": _IDENTITY,
    "user_permissions": _IDENTITY,
    "vault_access_log": "an access record of the source tenant's vault; it stays with the vault",
    "vault_resources": _SECRETS,
    "vault_secrets": _SECRETS,
    "workspace_seen": _WORKSPACE,
    "workspace_sync_state": _WORKSPACE,
}

#: Run states that mean the run is not finished. Moving one would leave the
#: engine that is executing it writing into a tenant that no longer holds it.
_LIVE_RUN_STATES = ("pending", "running", "awaiting_approval")
_LIVE_CODING_JOB_STATES = ("queued", "running")
#: Async work that outlives a finished run. An unresolved effect is resolved
#: by the source's recovery sweep against the source's CRM; an unfinished
#: memory write may be extracting into the source's memory right now.
_UNRESOLVED_EFFECT_STATES = ("prepared", "dispatching", "uncertain")
_UNFINISHED_MEMORY_WRITE_STATES = ("pending", "running")

_TENANT_TABLES_SQL = """
SELECT c.relname AS tbl FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
              AND a.attname = 'tenant_id' AND NOT a.attisdropped)
"""

#: Single-column foreign keys between two tenant-bearing tables.
_TENANT_FKS_SQL = """
SELECT cl.relname AS child, ca.attname AS col, ca.attnotnull AS notnull,
       fr.relname AS parent, fa.attname AS refcol
FROM pg_constraint c
JOIN pg_class cl ON cl.oid = c.conrelid
JOIN pg_class fr ON fr.oid = c.confrelid
JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = c.conkey[1]
JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = c.confkey[1]
WHERE c.contype = 'f' AND cardinality(c.conkey) = 1
  AND EXISTS (SELECT 1 FROM pg_attribute t WHERE t.attrelid = c.conrelid
              AND t.attname = 'tenant_id' AND NOT t.attisdropped)
  AND EXISTS (SELECT 1 FROM pg_attribute t WHERE t.attrelid = c.confrelid
              AND t.attname = 'tenant_id' AND NOT t.attisdropped)
ORDER BY 1, 2
"""

#: Unique indexes on plain columns that include ``tenant_id``: the only keys a
#: change of tenant can make collide.
_TENANT_UNIQUE_SQL = """
SELECT array_agg(a.attname ORDER BY k.ord) AS cols,
       pg_get_expr(x.indpred, x.indrelid) AS pred
FROM pg_index x
CROSS JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum
WHERE x.indrelid = %s::regclass AND x.indisunique AND x.indexprs IS NULL
  AND k.ord <= x.indnkeyatts
GROUP BY x.indexrelid, x.indpred, x.indrelid
HAVING bool_or(a.attname = 'tenant_id')
"""


class WorkloadMoveError(Exception):
    """The move was refused; nothing was written."""


@dataclass
class WorkloadMoveReport:
    """What :func:`move_workload` moved, or (``dry_run``) would move."""

    from_tenant: str
    to_tenant: str
    agents: list[str]
    workflows: list[str]
    dry_run: bool
    #: Per moved table, the rows re-tenanted (every table, zeros included).
    counts: dict[str, int] = dataclass_field(default_factory=dict)
    #: Per untenanted child table, the rows that follow a moved parent.
    followed: dict[str, int] = dataclass_field(default_factory=dict)
    #: ``table.column`` -> moved rows whose link to a row that stays in another
    #: tenant was cleared (only the non-zero ones).
    links_cleared: dict[str, int] = dataclass_field(default_factory=dict)
    audit_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _names(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return sorted({v.strip() for v in values or () if v and v.strip()})


def _ancestry(cur: Any, tenant: str) -> list[str]:
    """``tenant`` and its ancestors, nearest first; the last is its root."""
    cur.execute(
        """WITH RECURSIVE up(id, parent, depth) AS (
               SELECT id, parent_tenant_id, 0 FROM crm_tenants WHERE id = %s
               UNION ALL
               SELECT t.id, t.parent_tenant_id, up.depth + 1
               FROM crm_tenants t JOIN up ON t.id = up.parent
               WHERE up.depth < 32)
           SELECT id FROM up ORDER BY depth""",
        (tenant,),
    )
    return [r["id"] for r in cur.fetchall()]


def _refuse_tenants(cur: Any, source: str, target: str) -> str:
    """Raise for an unusable pair of tenants; return the root they share."""
    cur.execute("SELECT id, active FROM crm_tenants WHERE id IN (%s, %s)", (source, target))
    found = {r["id"]: r for r in cur.fetchall()}
    if source not in found:
        raise WorkloadMoveError(f"source tenant {source!r} does not exist")
    if target not in found:
        raise WorkloadMoveError(f"target tenant {target!r} does not exist")
    if source == target:
        raise WorkloadMoveError(f"--from and --to name the same tenant ({source!r})")
    if not found[target]["active"]:
        raise WorkloadMoveError(f"target tenant {target!r} is not active")
    up_source, up_target = _ancestry(cur, source), _ancestry(cur, target)
    if up_source[-1] != up_target[-1]:
        raise WorkloadMoveError(
            f"tenant {target!r} is not under the same root as {source!r} "
            f"({up_target[-1]!r}, not {up_source[-1]!r})"
        )
    if target in up_source:
        raise WorkloadMoveError(
            f"tenant {target!r} is an ancestor of {source!r}; a workload moves to a "
            "descendant or a sibling under the same root"
        )
    return up_source[-1]


def _shell_tenant() -> str:
    """The tenant this process's engine runs as: what a root shell must be."""
    from robothor.constants import DEFAULT_TENANT

    return (
        os.environ.get("ROBOTHOR_TENANT_ID", "")
        or os.environ.get("ROBOTHOR_DEFAULT_TENANT", "")
        or DEFAULT_TENANT
    )


def _refuse_foreign_shell(root: str) -> None:
    """The move is the root operator's; refuse a shell bound to any other tenant."""
    shell = _shell_tenant()
    if shell != root:
        raise WorkloadMoveError(
            f"this shell's engine tenant is {shell!r}, but both tenants are under the root "
            f"{root!r}; run move-workload from a root shell (ROBOTHOR_TENANT_ID unset or "
            f"{root!r}, and ROBOTHOR_DEFAULT_TENANT {root!r})"
        )


def _refuse_unclassified(cur: Any) -> None:
    cur.execute(_TENANT_TABLES_SQL)
    tables = {r["tbl"] for r in cur.fetchall()}
    known = set(MOVED_TABLES) | set(UNTOUCHED_TABLES) | set(REFUSED_IF_PRESENT)
    missing = sorted(tables - known)
    if missing:
        raise WorkloadMoveError(
            "tenant-bearing table(s) with no workload-move classification: "
            f"{', '.join(missing)}; this version cannot tell whether their rows are "
            "a workload's history. Upgrade, or classify them in "
            "robothor/tenancy/move_workload.py"
        )


def _select(cur: Any, params: dict[str, Any]) -> None:
    """Fill the temporary tables the predicates read.

    Runs: the named agents' runs, the runs the named workflows' steps started,
    and every run descended from either (sub-agent runs travel with their
    parent). Sessions: ``agent:<id>:…`` keys of the named agents, matched by
    segment, never by LIKE (an underscore in an agent id is a wildcard there).
    """
    for name in ("_mw_runs", "_mw_wf_runs", "_mw_sessions"):
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
    cur.execute(
        """CREATE TEMP TABLE _mw_wf_runs ON COMMIT DROP AS
           SELECT id FROM workflow_runs
           WHERE tenant_id = %(source)s AND workflow_id = ANY(%(workflows)s::text[])""",
        params,
    )
    cur.execute(
        """CREATE TEMP TABLE _mw_runs ON COMMIT DROP AS
           WITH RECURSIVE seed AS (
               SELECT id FROM agent_runs
               WHERE tenant_id = %(source)s AND agent_id = ANY(%(agents)s::text[])
               UNION
               SELECT s.agent_run_id FROM workflow_run_steps s
               WHERE s.run_id IN (SELECT id FROM _mw_wf_runs) AND s.agent_run_id IS NOT NULL
           ), tree AS (
               SELECT id FROM seed
               UNION
               SELECT r.id FROM agent_runs r JOIN tree ON r.parent_run_id = tree.id
           )
           SELECT DISTINCT r.id FROM tree JOIN agent_runs r
             ON r.id = tree.id AND r.tenant_id = %(source)s""",
        params,
    )
    cur.execute(
        """CREATE TEMP TABLE _mw_sessions ON COMMIT DROP AS
           SELECT id, session_key FROM chat_sessions
           WHERE tenant_id = %(source)s
             AND split_part(session_key, ':', 1) = 'agent'
             AND split_part(session_key, ':', 2) = ANY(%(agents)s::text[])""",
        params,
    )


def _in_source(table: str, predicate: str) -> sql.Composed:
    # ``predicate`` is a constant of this module, never caller input.
    return sql.SQL("FROM {} WHERE tenant_id = %(source)s AND ({})").format(
        sql.Identifier(table), sql.SQL(predicate)
    )


def _count(cur: Any, table: str, predicate: str, params: dict[str, Any]) -> int:
    cur.execute(sql.SQL("SELECT COUNT(*) AS n ") + _in_source(table, predicate), params)
    return int(cur.fetchone()["n"])


def _refuse_live(cur: Any, params: dict[str, Any]) -> None:
    live = {
        "agent_runs": _count(
            cur, "agent_runs", f"id IN {_RUNS} AND status IN {_LIVE_RUN_STATES}", params
        ),
        "workflow_runs": _count(
            cur, "workflow_runs", f"id IN {_WF_RUNS} AND status IN {_LIVE_RUN_STATES}", params
        ),
        "coding_jobs": _count(
            cur,
            "coding_jobs",
            f"run_id IN {_RUN_TEXT} AND status IN {_LIVE_CODING_JOB_STATES}",
            params,
        ),
        "agent_runtime_effects": _count(
            cur,
            "agent_runtime_effects",
            f"run_id IN {_RUN_TEXT} AND state IN {_UNRESOLVED_EFFECT_STATES}",
            params,
        ),
        "memory_write_jobs": _count(
            cur,
            "memory_write_jobs",
            f"run_id IN {_RUN_TEXT} AND status IN {_UNFINISHED_MEMORY_WRITE_STATES}",
            params,
        ),
    }
    listed = ", ".join(f"{t}={n}" for t, n in live.items() if n)
    if listed:
        raise WorkloadMoveError(
            f"the workload is still running in {params['source']!r} ({listed}); "
            "let it finish, or cancel it, before moving its history"
        )


def _refuse_present(cur: Any, params: dict[str, Any]) -> None:
    for table, (predicate, reason) in REFUSED_IF_PRESENT.items():
        n = _count(cur, table, predicate, params)
        if n:
            raise WorkloadMoveError(f"the workload has {n} row(s) in {table}: {reason}")


def _refuse_collisions(cur: Any, params: dict[str, Any]) -> None:
    """Refuse when a moved row's unique key already exists in the target."""
    clashes: list[str] = []
    for table, predicate in MOVED_TABLES.items():
        cur.execute(_TENANT_UNIQUE_SQL, (table,))
        for index in cur.fetchall():
            cols = [c for c in index["cols"] if c != "tenant_id"]
            # An index predicate comes from the catalog; escape it for the
            # driver's %-parameters.
            index_pred = sql.SQL((index["pred"] or "TRUE").replace("%", "%%"))
            not_null = sql.SQL(" AND ").join(
                [sql.SQL("{} IS NOT NULL").format(sql.Identifier(c)) for c in cols]
                or [sql.SQL("TRUE")]
            )
            select = sql.SQL(", ").join([sql.Identifier(c) for c in cols] or [sql.SQL("1 AS one")])
            query = sql.SQL(
                "SELECT {sel} FROM {t} WHERE tenant_id = %(source)s AND ({p}) AND ({ip}) "
                "AND {nn} INTERSECT "
                "SELECT {sel} FROM {t} WHERE tenant_id = %(target)s AND ({ip}) AND {nn} LIMIT 5"
            ).format(
                sel=select,
                t=sql.Identifier(table),
                p=sql.SQL(predicate),
                ip=index_pred,
                nn=not_null,
            )
            cur.execute(query, params)
            for row in cur.fetchall():
                keys = ", ".join(f"{c}={row[c]}" for c in cols) or "(one row per tenant)"
                clashes.append(f"{table} ({keys})")
    if clashes:
        raise WorkloadMoveError(
            f"tenant {params['target']!r} already has rows with the same keys as the "
            f"workload's: {'; '.join(clashes)}. Nothing was moved; remove or rename "
            "those rows in the target first"
        )


def _foreign_keys(cur: Any) -> list[dict[str, Any]]:
    cur.execute(_TENANT_FKS_SQL)
    return [dict(r) for r in cur.fetchall()]


def _refuse_left_behind(cur: Any, fks: list[dict[str, Any]], params: dict[str, Any]) -> None:
    """Refuse when a source row that stays references a row that moves."""
    # ``workflow_run_steps.agent_run_id`` has no foreign key, so the catalog
    # loop below cannot see a step of a workflow that is not named pointing at
    # a run that moves.
    cur.execute(
        "SELECT COUNT(*) AS n FROM workflow_run_steps s "
        "JOIN workflow_runs w ON w.id = s.run_id AND w.tenant_id = %(source)s "
        f"WHERE s.agent_run_id IN {_RUNS} AND s.run_id NOT IN {_WF_RUNS}",
        params,
    )
    n = int(cur.fetchone()["n"])
    if n:
        raise WorkloadMoveError(
            f"{n} workflow_run_steps row(s) of a workflow that is not being moved started "
            "runs of the workload; name that workflow with --workflow too, or the steps "
            "would point across tenants"
        )
    for fk in fks:
        parent, child = fk["parent"], fk["child"]
        if parent not in MOVED_TABLES:
            continue
        # NULL-safe: a row whose predicate is NULL is not moved, so it stays.
        stays = f"({MOVED_TABLES[child]}) IS NOT TRUE" if child in MOVED_TABLES else "TRUE"
        query = sql.SQL(
            "SELECT COUNT(*) AS n FROM {c} WHERE tenant_id = %(source)s AND ({stays}) "
            "AND {col} IN (SELECT {ref} {moved})"
        ).format(
            c=sql.Identifier(child),
            stays=sql.SQL(stays),
            col=sql.Identifier(fk["col"]),
            ref=sql.Identifier(fk["refcol"]),
            moved=_in_source(parent, MOVED_TABLES[parent]),
        )
        cur.execute(query, params)
        n = int(cur.fetchone()["n"])
        if n:
            raise WorkloadMoveError(
                f"{n} row(s) of {child} stay in {params['source']!r} but reference "
                f"{parent} rows of the workload ({child}.{fk['col']}); moving would "
                "leave them pointing across tenants"
            )


def _links(
    cur: Any, fks: list[dict[str, Any]], params: dict[str, Any], *, clear: bool
) -> dict[str, int]:
    """Moved rows whose foreign key names a row that will not be in the target.

    The link is cleared (``clear``) or only counted. A row "will be in the
    target" when it is there already or is itself part of the move.
    """
    found: dict[str, int] = {}
    for fk in fks:
        child, parent = fk["child"], fk["parent"]
        if child not in MOVED_TABLES:
            continue
        col, ref = sql.Identifier(fk["col"]), sql.Identifier(fk["refcol"])
        stays_out = sql.SQL(
            "NOT EXISTS (SELECT 1 FROM {p} r WHERE r.{ref} = m.{col} AND r.tenant_id = %(target)s)"
        ).format(p=sql.Identifier(parent), ref=ref, col=col)
        if parent in MOVED_TABLES:
            stays_out += sql.SQL(
                " AND NOT EXISTS (SELECT 1 FROM (SELECT {ref} AS k {moved}) mv WHERE mv.k = m.{col})"
            ).format(ref=ref, moved=_in_source(parent, MOVED_TABLES[parent]), col=col)
        where = sql.SQL(
            "WHERE m.tenant_id = %(source)s AND ({pred}) AND m.{col} IS NOT NULL AND {out}"
        ).format(pred=sql.SQL(MOVED_TABLES[child]), col=col, out=stays_out)
        target = sql.SQL("{} AS m ").format(sql.Identifier(child))
        cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM ") + target + where, params)
        n = int(cur.fetchone()["n"])
        if not n:
            continue
        if fk["notnull"]:
            raise WorkloadMoveError(
                f"{n} row(s) of {child} reference {parent} rows that stay outside "
                f"{params['target']!r} through the required column {fk['col']}"
            )
        if clear:
            cur.execute(
                sql.SQL("UPDATE ") + target + sql.SQL("SET {} = NULL ").format(col) + where,
                params,
            )
        key = f"{child}.{fk['col']}"
        found[key] = found.get(key, 0) + n
    return found


def _followed(cur: Any, params: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child, (parent, column) in FOLLOWS_PARENT.items():
        query = sql.SQL("SELECT COUNT(*) AS n FROM {c} WHERE {col} IN (SELECT id {moved})").format(
            c=sql.Identifier(child),
            col=sql.Identifier(column),
            moved=_in_source(parent, MOVED_TABLES[parent]),
        )
        cur.execute(query, params)
        counts[child] = int(cur.fetchone()["n"])
    return counts


def _record_audit(cur: Any, *, actor: str, root: str, report: WorkloadMoveReport) -> int:
    """One ``tenant.workload_moved`` row, filed under the source's root tenant.

    The root operator runs the move and reads the trail there; ``details``
    names both tenants. Rows already in ``audit_log`` are never re-tenanted.
    """
    details = {k: v for k, v in report.to_dict().items() if k not in ("audit_id", "dry_run")}
    cur.execute(
        "INSERT INTO audit_log (event_type, category, actor, action, details, target, "
        "status, tenant_id) VALUES ('tenant.workload_moved', 'tenant', %s, %s, %s, %s, "
        "'ok', %s) RETURNING id",
        (
            actor,
            f"move workload from {report.from_tenant} to {report.to_tenant}",
            Json(details),
            f"tenant:{report.to_tenant}",
            root,
        ),
    )
    return int(cur.fetchone()["id"])


def _one_snapshot(conn: Any) -> None:
    """Run the move at REPEATABLE READ: every statement sees one snapshot.

    At READ COMMITTED each UPDATE would see rows the source engine committed
    after the workload's runs were selected, so a new run would stay behind
    while its agent-keyed rows moved. Under one snapshot those rows stay
    together, and a concurrent update of a moving row fails the move (rolled
    back) instead of splitting it. The checkout's own statements (its tenant
    binding) are committed first, because the level must be set before a
    transaction's first query; their session settings survive the commit.
    """
    if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
        conn.commit()
    if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
        # Only a caller that owns the transaction can get here (the RLS test
        # wrapper, whose commit is a no-op); it keeps its own isolation.
        logger.warning("move_workload: an open transaction keeps its isolation level")
        return
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def move_workload(
    *,
    from_tenant: str,
    to_tenant: str,
    agents: list[str] | tuple[str, ...],
    workflows: list[str] | tuple[str, ...],
    actor: str,
    dry_run: bool = False,
) -> WorkloadMoveReport:
    """Move the named agents' and workflows' history to another tenant.

    ONE transaction, under :func:`read_every_tenant_in_transaction` (the move
    reads the source tenant and writes the target; a connection bound to
    either would see half of it). Every refusal is checked before the first
    write: the tenants (both exist, the target is active, both sit under the
    same root, the target is a descendant or sibling, never an ancestor, and
    this shell's engine tenant is that root),
    every tenant table is classified, nothing of the workload is still running,
    nothing in :data:`REFUSED_IF_PRESENT` is present, no moved row's unique key
    already exists in the target, and no row that stays references a row that
    moves. Then each table in :data:`MOVED_TABLES` is re-tenanted in place, a
    moved row's link to a row that stays in another tenant is cleared, and one
    ``audit_log`` row records the move (none when nothing moved, as on a
    re-run). Any failure rolls all of it back.

    ``dry_run`` runs every refusal and reports the same counts, then rolls back.
    Raises ``WorkloadMoveError`` for a refusal; database errors propagate after
    the rollback.
    """
    agent_ids, workflow_ids = _names(agents), _names(workflows)
    if not agent_ids and not workflow_ids:
        raise WorkloadMoveError("name at least one --agent or --workflow to move")
    params: dict[str, Any] = {
        "source": from_tenant,
        "target": to_tenant,
        "agents": agent_ids,
        "workflows": workflow_ids,
    }
    report = WorkloadMoveReport(
        from_tenant=from_tenant,
        to_tenant=to_tenant,
        agents=agent_ids,
        workflows=workflow_ids,
        dry_run=dry_run,
    )
    with get_connection() as conn:
        try:
            _one_snapshot(conn)
            read_every_tenant_in_transaction(conn)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            root = _refuse_tenants(cur, from_tenant, to_tenant)
            _refuse_foreign_shell(root)
            _refuse_unclassified(cur)
            _select(cur, params)
            _refuse_live(cur, params)
            _refuse_present(cur, params)
            _refuse_collisions(cur, params)
            fks = _foreign_keys(cur)
            _refuse_left_behind(cur, fks, params)
            report.followed = _followed(cur, params)
            report.links_cleared = _links(cur, fks, params, clear=not dry_run)

            if dry_run:
                report.counts = {
                    table: _count(cur, table, predicate, params)
                    for table, predicate in MOVED_TABLES.items()
                }
                conn.rollback()
                return report

            for table, predicate in MOVED_TABLES.items():
                cur.execute(
                    sql.SQL("UPDATE {} SET tenant_id = %(target)s ").format(sql.Identifier(table))
                    + sql.SQL("WHERE tenant_id = %(source)s AND ({})").format(sql.SQL(predicate)),
                    params,
                )
                report.counts[table] = int(cur.rowcount or 0)
            # A re-run of a move that already happened changes nothing and
            # records nothing: the trail lists only moves that moved rows.
            if any(report.counts.values()) or report.links_cleared:
                report.audit_id = _record_audit(cur, actor=actor, root=root, report=report)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info(
        "move_workload: %s moved %s/%s from %s to %s (%s)",
        actor,
        agent_ids,
        workflow_ids,
        from_tenant,
        to_tenant,
        {t: n for t, n in report.counts.items() if n},
    )
    return report
