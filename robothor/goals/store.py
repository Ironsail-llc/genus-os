"""Transactional goal storage. Every operation is explicitly tenant scoped."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
from uuid import UUID, uuid4

from psycopg2.extras import Json, RealDictCursor

from robothor.crm.dal import benchmark_sandbox_active
from robothor.db.connection import get_connection
from robothor.goals.model import (
    INACTIVE,
    CreateGoal,
    GoalUpdate,
    future,
    new_goal,
    now_iso,
    transition,
)


@contextmanager
def transaction() -> Iterator[Any]:
    if benchmark_sandbox_active():
        raise ValueError("goal pursuit is unavailable in the benchmark sandbox")
    with get_connection() as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def journal(
    cur: Any, tenant: str, goal: dict[str, Any], action: str, actor: str, detail: dict[str, Any]
) -> None:
    cur.execute(
        """INSERT INTO pursuit_goal_history(tenant_id,goal_id,action,actor,detail)
                   VALUES (%s,%s,%s,%s,%s)""",
        (tenant, goal["id"], action, actor, Json(detail)),
    )


def save(cur: Any, tenant: str, g: dict[str, Any]) -> None:
    cur.execute(
        """UPDATE pursuit_goals SET data=%s,status=%s,ready_at=%s,priority=%s
                   WHERE tenant_id=%s AND id=%s""",
        (Json(g), g["status"], g["ready_at"], g["priority"], tenant, g["id"]),
    )


def locked(cur: Any, tenant: str, goal_id: str) -> dict[str, Any]:
    UUID(goal_id)
    cur.execute(
        "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND id=%s FOR UPDATE", (tenant, goal_id)
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("goal not found")
    return dict(row["data"])


def enabled(tenant: str) -> bool:
    with transaction() as cur:
        cur.execute("SELECT enabled FROM goal_pursuit_settings WHERE tenant_id=%s", (tenant,))
        row = cur.fetchone()
        return bool(row and row["enabled"])


def set_enabled(tenant: str, value: bool, actor: str) -> None:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO goal_pursuit_settings(tenant_id,enabled,updated_by)
                       VALUES (%s,%s,%s) ON CONFLICT(tenant_id) DO UPDATE
                       SET enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=now()""",
            (tenant, value, actor),
        )


def _create(cur: Any, tenant: str, spec: CreateGoal, actor: str) -> dict[str, Any]:
    import hashlib
    import json

    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
    parent = locked(cur, tenant, spec.parent_goal_id) if spec.parent_goal_id else None
    key = spec.request_key
    if not key and spec.parent_goal_id:
        key = (
            "child:"
            + hashlib.sha256(
                json.dumps(
                    [
                        spec.parent_goal_id,
                        spec.objective,
                        spec.success_criteria,
                        (parent.get("assessment") or {}).get("at") if parent else None,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
    if key:
        cur.execute(
            "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND data->>'request_key'=%s",
            (tenant, key),
        )
        existing = cur.fetchone()
        if existing:
            return dict(existing["data"])
    if spec.parent_goal_id:
        parent = locked(cur, tenant, spec.parent_goal_id)
        if parent["kind"] != "long" or parent["status"] in INACTIVE:
            raise ValueError("parent must be an active long-term goal")
        if spec.kind != "short":
            raise ValueError("execution children must be short-term goals")
    g = new_goal(spec, actor)
    g["request_key"] = key
    cur.execute(
        """INSERT INTO pursuit_goals(tenant_id,id,data,status,priority)
                   VALUES (%s,%s,%s,%s,%s)""",
        (tenant, g["id"], Json(g), g["status"], g["priority"]),
    )
    journal(cur, tenant, g, "create", actor, g)
    return g


def create(tenant: str, spec: CreateGoal, actor: str) -> dict[str, Any]:
    with transaction() as cur:
        return _create(cur, tenant, spec, actor)


def get(tenant: str, goal_id: str) -> dict[str, Any]:
    with transaction() as cur:
        g = locked(cur, tenant, goal_id)
        cur.execute(
            """SELECT action,actor,detail,created_at FROM pursuit_goal_history
                       WHERE tenant_id=%s AND goal_id=%s ORDER BY id DESC LIMIT 100""",
            (tenant, goal_id),
        )
        g["history"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT t.id,t.title,t.status,t.resolution FROM pursuit_goal_tasks l JOIN crm_tasks t
                       ON t.id=l.task_id AND t.tenant_id=l.tenant_id
                       WHERE l.tenant_id=%s AND l.goal_id=%s""",
            (tenant, goal_id),
        )
        g["tasks"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT id,run_id,status,tokens,cost_usd FROM pursuit_goal_attempts
                       WHERE tenant_id=%s AND goal_id=%s ORDER BY started_at DESC LIMIT 20""",
            (tenant, goal_id),
        )
        g["runs"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND data->>'parent_goal_id'=%s ORDER BY ready_at",
            (tenant, goal_id),
        )
        g["children"] = [r["data"] for r in cur.fetchall()]
        from robothor.goals.effect_summary import summary

        g["action_evidence"] = summary(cur, tenant, goal_id)
        return g


def list_goals(tenant: str, *, task_summary: bool = False) -> list[dict[str, Any]]:
    with transaction() as cur:
        cur.execute(
            "SELECT data FROM pursuit_goals WHERE tenant_id=%s ORDER BY ready_at DESC LIMIT 200",
            (tenant,),
        )
        goals = [r["data"] for r in cur.fetchall()]
        if goals:
            from robothor.goals.effect_summary import attach

            attach(cur, tenant, goals)
        if task_summary and goals:
            from robothor.goals.task_summary import attach_task_summaries

            attach_task_summaries(cur, tenant, goals)
        return goals


def update(
    tenant: str, goal_id: str, change: GoalUpdate, actor: str, *, operator: bool = False
) -> dict[str, Any]:
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
        before = locked(cur, tenant, goal_id)
        if change.action in {"complete", "approve", "assess", "reconciled"}:
            from robothor.goals.effect_summary import require_settled

            require_settled(cur, tenant, goal_id)
        if change.action == "complete":
            cur.execute(
                """SELECT 1 FROM pursuit_goals WHERE tenant_id=%s
                           AND data->>'parent_goal_id'=%s AND status NOT IN ('complete','canceled') LIMIT 1""",
                (tenant, goal_id),
            )
            if cur.fetchone():
                raise ValueError(
                    "finish or cancel outstanding execution children before completing"
                )
        g = transition(before, change, operator=operator)
        if change.action == "resume" and g["parent_goal_id"]:
            parent = locked(cur, tenant, g["parent_goal_id"])
            if parent["status"] in INACTIVE:
                raise ValueError("make the parent active before resuming this child goal")
        if change.action == "evidence":
            from robothor.goals.evidence import verify

            g["evidence"][-1]["verification"] = (
                verify(cur, tenant, before, change.reference, change.criterion)
                if change.satisfied
                else {"method": "negative_assessment", "independent": False}
            )
        if change.action == "block":
            from robothor.goals.runtime import binding

            current = binding.get()
            if current and current.goal_id == goal_id:
                if (
                    before.get("last_block_attempt") == current.attempt
                    and before["blocker"] == change.note
                ):
                    return before
                g["last_block_attempt"] = current.attempt
        if change.action == "link_task" or (change.action == "wait" and change.task_id):
            cur.execute(
                "SELECT id FROM crm_tasks WHERE tenant_id=%s AND id=%s AND deleted_at IS NULL",
                (tenant, change.task_id),
            )
            if not cur.fetchone():
                raise ValueError("task not found in this tenant")
            cur.execute(
                """INSERT INTO pursuit_goal_tasks(tenant_id,goal_id,task_id) VALUES (%s,%s,%s)
                           ON CONFLICT DO NOTHING""",
                (tenant, goal_id, change.task_id),
            )
        save(cur, tenant, g)
        journal(cur, tenant, g, change.action, actor, change.model_dump(mode="json"))
        if before["status"] != g["status"] and g["status"] in {"complete", "review", "blocked"}:
            notify(cur, tenant, g, g["status"], change.note)
        if change.action == "assess" and (before.get("assessment") or {}).get("status") != (
            g.get("assessment") or {}
        ).get("status"):
            notify(cur, tenant, g, "assessment changed", change.note)
        if before["kind"] != g["kind"]:
            notify(cur, tenant, g, "promoted to long-term", change.note)
            journal(cur, tenant, g, "promoted", "engine", {"reason": change.note})
        if g["parent_goal_id"] and g["status"] in {
            "waiting",
            "complete",
            "blocked",
            "canceled",
            "review",
        }:
            parent = locked(cur, tenant, g["parent_goal_id"])
            if parent["status"] not in INACTIVE:
                parent.update(status="queued", ready_at=now_iso(), version=parent["version"] + 1)
                parent["next_action"] = f"Reassess child {g['id']}: {g['status']}. {change.note}"
                save(cur, tenant, parent)
                journal(
                    cur,
                    tenant,
                    parent,
                    "child_changed",
                    "engine",
                    {"child": g["id"], "status": g["status"]},
                )
        if change.action == "resume":
            cur.execute(
                "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND data->>'paused_by_parent'=%s FOR UPDATE",
                (tenant, goal_id),
            )
            for row in cur.fetchall():
                child = row["data"]
                if child["status"] == "paused":
                    restored = child.pop("before_parent_pause", "queued")
                    child.update(
                        status="queued" if restored == "running" else restored,
                        version=child["version"] + 1,
                    )
                    child.pop("paused_by_parent", None)
                    save(cur, tenant, child)
                    journal(cur, tenant, child, "resume", actor, {"parent": goal_id})
        if change.action in {"pause", "cancel"}:
            cur.execute(
                "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND data->>'parent_goal_id'=%s FOR UPDATE",
                (tenant, goal_id),
            )
            for row in cur.fetchall():
                child = row["data"]
                if child["status"] not in {"complete", "canceled"}:
                    if change.action == "pause" and child["status"] != "paused":
                        child["paused_by_parent"] = goal_id
                        child["before_parent_pause"] = child["status"]
                    child.update(status=g["status"], version=child["version"] + 1)
                    save(cur, tenant, child)
                    journal(cur, tenant, child, change.action, actor, {"parent": goal_id})
    if change.action in {"pause", "cancel"}:
        from robothor.engine.runtime.activity import stop_goal

        stop_goal(tenant, goal_id)
    return g


def ingest_event(tenant: str, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO pursuit_goal_events(tenant_id,id,event_type,payload) VALUES (%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
            (tenant, event_id, event_type, Json(payload)),
        )


def reconcile(cur: Any, tenant: str) -> None:
    # A dead run is not retried blindly: the next turn must inspect the attempt and tool ledger.
    cur.execute(
        "SELECT data,lease_id FROM pursuit_goals WHERE tenant_id=%s AND lease_until<now() FOR UPDATE",
        (tenant,),
    )
    for row in cur.fetchall():
        g = row["data"]
        cur.execute(
            "SELECT tokens,cost_usd FROM pursuit_goal_attempts WHERE tenant_id=%s AND id=%s AND status='running'",
            (tenant, row["lease_id"]),
        )
        usage = cur.fetchone()
        if usage:
            g["tokens_used"] += usage["tokens"]
            g["cost_usd"] += usage["cost_usd"]
            if g["parent_goal_id"]:
                parent = locked(cur, tenant, g["parent_goal_id"])
                parent["tokens_used"] += usage["tokens"]
                parent["cost_usd"] += usage["cost_usd"]
                parent["version"] += 1
                save(cur, tenant, parent)
        g.update(recovery_required=True, version=g["version"] + 1)
        if g["status"] not in INACTIVE:
            g.update(status="queued", ready_at=now_iso())
        save(cur, tenant, g)
        cur.execute(
            "UPDATE pursuit_goals SET lease_id=NULL,lease_until=NULL WHERE tenant_id=%s AND id=%s",
            (tenant, g["id"]),
        )
        cur.execute(
            """UPDATE pursuit_goal_attempts SET status='interrupted',finished_at=now()
                       WHERE tenant_id=%s AND id=%s AND status='running'""",
            (tenant, row["lease_id"]),
        )
        journal(cur, tenant, g, "recovered", "engine", {"attempt": str(row["lease_id"])})
    cur.execute(
        "SELECT * FROM pursuit_goal_events WHERE tenant_id=%s AND processed_at IS NULL ORDER BY created_at LIMIT 500",
        (tenant,),
    )
    events = cur.fetchall()
    cur.execute(
        "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND status='waiting' FOR UPDATE",
        (tenant,),
    )
    waiting = [r["data"] for r in cur.fetchall()]
    for g in waiting:
        wait = g.get("wait") or {}
        for event in events:
            if wait.get("registered_at") and event["created_at"] < datetime.fromisoformat(
                wait["registered_at"]
            ):
                continue
            payload = event["payload"]
            task_match = payload.get("task_id") and payload.get("task_id") == wait.get("task_id")
            if event["event_type"] == "task.changed" and not task_match:
                cur.execute(
                    "SELECT 1 FROM pursuit_goal_tasks WHERE tenant_id=%s AND goal_id=%s AND task_id::text=%s",
                    (tenant, g["id"], payload.get("task_id")),
                )
                task_match = bool(cur.fetchone())
            event_match = wait.get("event_type") == event["event_type"] and all(
                k in payload and str(payload[k]) == v
                for k, v in wait.get("event_match", {}).items()
            )
            if task_match or event_match:
                g.update(status="queued", ready_at=now_iso(), version=g["version"] + 1)
                g["next_action"] = f"Reassess wake event {event['event_type']}: {payload}"
                save(cur, tenant, g)
                journal(cur, tenant, g, "wake", "engine", {"event_id": event["id"]})
                break
    for event in events:
        cur.execute(
            "UPDATE pursuit_goal_events SET processed_at=now() WHERE tenant_id=%s AND id=%s",
            (tenant, event["id"]),
        )


def claim(tenant: str) -> tuple[dict[str, Any], str] | None:
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
        cur.execute("SELECT enabled FROM goal_pursuit_settings WHERE tenant_id=%s", (tenant,))
        flag = cur.fetchone()
        if not flag or not flag["enabled"]:
            return None
        reconcile(cur, tenant)
        cur.execute(
            "SELECT 1 FROM pursuit_goals WHERE tenant_id=%s AND lease_id IS NOT NULL", (tenant,)
        )
        if cur.fetchone():
            return None
        cur.execute(
            """SELECT data FROM pursuit_goals g WHERE tenant_id=%s
                       AND status IN ('queued','waiting') AND ready_at<=clock_timestamp()
                       AND NOT EXISTS (SELECT 1 FROM pursuit_goals p WHERE p.tenant_id=g.tenant_id
                           AND p.id::text=g.data->>'parent_goal_id'
                           AND p.status IN ('paused','blocked','review','complete','canceled'))
                       ORDER BY priority DESC,ready_at ASC LIMIT 1 FOR UPDATE""",
            (tenant,),
        )
        row = cur.fetchone()
        if not row:
            return None
        g = row["data"]
        if g["token_budget"] and g["tokens_used"] >= g["token_budget"]:
            g.update(status="blocked", blocker="token budget exhausted", version=g["version"] + 1)
            save(cur, tenant, g)
            journal(cur, tenant, g, "budget_exhausted", "engine", {})
            return None
        attempt = str(uuid4())
        g.update(status="running", version=g["version"] + 1, attempts=g["attempts"] + 1)
        g["attempt_progress"] = g.get("progress_revision", 0)
        save(cur, tenant, g)
        cur.execute(
            "UPDATE pursuit_goals SET lease_id=%s,lease_until=now()+interval '90 seconds' WHERE tenant_id=%s AND id=%s",
            (attempt, tenant, g["id"]),
        )
        cur.execute(
            "INSERT INTO pursuit_goal_attempts(tenant_id,id,goal_id) VALUES (%s,%s,%s)",
            (tenant, attempt, g["id"]),
        )
        journal(cur, tenant, g, "dispatch", "engine", {"attempt": attempt})
        return g, attempt


def heartbeat(
    tenant: str, goal_id: str, attempt: str, *, tokens: int | None = None, cost: float | None = None
) -> bool:
    with transaction() as cur:
        cur.execute(
            """UPDATE pursuit_goals SET lease_until=now()+interval '90 seconds'
                       WHERE tenant_id=%s AND id=%s AND lease_id=%s AND status NOT IN
                       ('paused','canceled','blocked') AND EXISTS
                       (SELECT 1 FROM goal_pursuit_settings WHERE tenant_id=%s AND enabled)
                       RETURNING id""",
            (tenant, goal_id, attempt, tenant),
        )
        held = bool(cur.fetchone())
        if held and tokens is not None:
            cur.execute(
                """UPDATE pursuit_goal_attempts SET tokens=GREATEST(tokens,%s),
                           cost_usd=GREATEST(cost_usd,%s) WHERE tenant_id=%s AND id=%s AND status='running'""",
                (tokens, cost or 0, tenant, attempt),
            )
        return held


def finish(
    tenant: str,
    goal_id: str,
    attempt: str,
    *,
    run_id: str | None = None,
    tokens: int = 0,
    cost: float = 0,
    error: str = "",
    budget_exhausted: bool = False,
    checkpoint: str = "",
    interrupted: bool = False,
) -> None:
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
        cur.execute(
            "SELECT data FROM pursuit_goals WHERE tenant_id=%s AND id=%s AND lease_id=%s FOR UPDATE",
            (tenant, goal_id, attempt),
        )
        row = cur.fetchone()
        if not row:
            return  # stale worker cannot overwrite the new owner's state
        g = row["data"]
        previous_status = g["status"]
        g["tokens_used"] += max(0, tokens)
        g["cost_usd"] += max(0, cost)
        if g["parent_goal_id"]:
            parent = locked(cur, tenant, g["parent_goal_id"])
            parent["tokens_used"] += max(0, tokens)
            parent["cost_usd"] += max(0, cost)
            parent["version"] += 1
            save(cur, tenant, parent)
        if interrupted:
            g["recovery_required"] = True
        if checkpoint:
            g["last_output"] = checkpoint[-20000:]
        if g["status"] == "running":
            g.update(status="queued", ready_at=now_iso())
            if error:
                g["failures"] += 1
                g["ready_at"] = future(min(300, 15 * 2 ** min(g["failures"], 4)))
                if g["failures"] >= 3:
                    g.update(status="blocked", blocker=error)
            else:
                g["failures"] = 0
                g["no_progress_runs"] = (
                    g.get("no_progress_runs", 0) + 1
                    if g.get("progress_revision", 0) == g.get("attempt_progress", 0)
                    else 0
                )
                if g["no_progress_runs"] >= 3:
                    g.update(
                        status="blocked", blocker="No progress checkpoint or evidence in three runs"
                    )
            if budget_exhausted or (g["token_budget"] and g["tokens_used"] >= g["token_budget"]):
                g.update(status="blocked", blocker="execution budget exhausted")
        g.update(version=g["version"] + 1, updated_at=now_iso())
        if previous_status != "blocked" and g["status"] == "blocked":
            notify(cur, tenant, g, "blocked", g["blocker"])
            if g["parent_goal_id"]:
                parent = locked(cur, tenant, g["parent_goal_id"])
                if parent["status"] not in INACTIVE:
                    parent.update(
                        status="queued",
                        ready_at=now_iso(),
                        version=parent["version"] + 1,
                        next_action=f"Reassess blocked child {g['id']}: {g['blocker']}",
                    )
                    save(cur, tenant, parent)
        save(cur, tenant, g)
        cur.execute(
            "UPDATE pursuit_goals SET lease_id=NULL,lease_until=NULL WHERE tenant_id=%s AND id=%s",
            (tenant, goal_id),
        )
        cur.execute(
            """UPDATE pursuit_goal_attempts SET status=%s,run_id=%s,tokens=%s,cost_usd=%s,finished_at=now()
                       WHERE tenant_id=%s AND id=%s""",
            ("failed" if error else "finished", run_id, tokens, cost, tenant, attempt),
        )
        journal(
            cur,
            tenant,
            g,
            "run_finished",
            "engine",
            {
                "run_id": run_id,
                "error": error,
                "tokens": tokens,
                "cost_usd": cost,
                "status": g["status"],
            },
        )


def control(tenant: str, goal_id: str) -> dict[str, Any]:
    with transaction() as cur:
        return locked(cur, tenant, goal_id)


def notify(cur: Any, tenant: str, g: dict[str, Any], event: str, detail: str) -> None:
    cur.execute(
        """INSERT INTO crm_agent_notifications
                   (id,tenant_id,from_agent,to_agent,notification_type,subject,body,metadata)
                   VALUES (%s,%s,'goal-pursuit','main','info',%s,%s,%s)""",
        (
            str(uuid4()),
            tenant,
            f"Goal {event}: {g['objective'][:150]}",
            detail,
            Json({"goal_id": g["id"], "goal_event": event}),
        ),
    )


def adopt(tenant: str, task_id: str, actor: str) -> dict[str, Any]:
    with transaction() as cur:
        cur.execute(
            "SELECT * FROM crm_tasks WHERE tenant_id=%s AND id=%s AND 'session_goal'=ANY(tags)",
            (tenant, task_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("legacy goal not found")
        meta = row.get("session_goal_meta") or {}
        spec = CreateGoal(
            objective=row.get("objective") or row["title"],
            success_criteria=meta.get("success_criteria") or [row.get("objective") or row["title"]],
            kind="long",
            request_key="legacy:" + task_id,
        )
        goal = _create(cur, tenant, spec, actor)
        if goal.get("legacy_task_id"):
            return goal
        goal["legacy_task_id"] = task_id
        goal["legacy_evidence"] = meta.get("evidence", [])
        save(cur, tenant, goal)
        journal(
            cur,
            tenant,
            goal,
            "adopt",
            actor,
            {"legacy_task_id": task_id, "evidence": goal["legacy_evidence"]},
        )
    return goal


def reserve_provider_usage(tenant: str, goal_id: str, attempt: str, tokens: int) -> None:
    """Durably retain worst-case attempt usage before a provider request can leave."""
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
        cur.execute(
            """SELECT g.data FROM pursuit_goals g JOIN goal_pursuit_settings s
                    ON s.tenant_id=g.tenant_id WHERE g.tenant_id=%s AND g.id=%s AND g.lease_id=%s
                    AND g.lease_until>now() AND s.enabled AND g.status IN ('running','queued') FOR UPDATE OF g""",
            (tenant, goal_id, attempt),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("goal lease no longer authorizes provider spending")
        goal = row["data"]
        budgets = [goal]
        if goal["parent_goal_id"]:
            budgets.append(locked(cur, tenant, goal["parent_goal_id"]))
        if any(
            g["status"] in INACTIVE
            or (g["token_budget"] and g["tokens_used"] + tokens > g["token_budget"])
            for g in budgets
        ):
            raise ValueError("goal family budget or authority exhausted")
        cur.execute(
            """UPDATE pursuit_goal_attempts SET tokens=GREATEST(tokens,%s)
                    WHERE tenant_id=%s AND id=%s AND status='running' RETURNING id""",
            (tokens, tenant, attempt),
        )
        if not cur.fetchone():
            raise ValueError("goal attempt no longer active")
