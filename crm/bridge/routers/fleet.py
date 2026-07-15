"""Operator-scoped, read-only fleet accounting.

For each agent it shows what it CAN do (tools, exec allowlist, sandbox, delivery)
beside what it DID (schedule state, 7d run/failure counts). The honesty carry-over
from Phase 1 is ``findings``: a capability without a constraint (exec with no
allowlist) is flagged, exactly as an inert control is.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

from robothor.db.connection import get_connection
from robothor.engine.config import load_all_manifests
from routers._operator import require_operator

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

_MANIFEST_DIR = Path(
    os.environ.get("ROBOTHOR_AGENTS_DIR")
    or (
        Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        / "docs"
        / "agents"
    )
)


def _load_manifests() -> list[dict]:
    return load_all_manifests(_MANIFEST_DIR)


def _schedule_rows() -> dict[str, dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT agent_id, enabled, next_run_at, last_run_at, last_status, "
                "consecutive_errors FROM agent_schedules"
            )
            return {r["agent_id"]: dict(r) for r in cur.fetchall()}


def _run_stats() -> dict[str, dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT agent_id, "
                "COUNT(*) AS runs_7d, "
                "COUNT(*) FILTER (WHERE status IN ('failed','timeout')) AS failures_7d "
                "FROM agent_runs WHERE started_at > now() - interval '7 days' "
                "GROUP BY agent_id"
            )
            return {
                r["agent_id"]: {"runs_7d": r["runs_7d"], "failures_7d": r["failures_7d"]}
                for r in cur.fetchall()
            }


def _findings(
    tools_allowed: list[str], exec_allowlist: list[str], sandbox: str | None
) -> list[dict]:
    has_exec = ("exec" in tools_allowed) or (not tools_allowed)
    constrained = bool(exec_allowlist)
    sandboxed = sandbox not in (None, "", "host", "local")
    out: list[dict] = []
    if has_exec and not constrained:
        out.append(
            {
                "code": "EXEC_NO_ALLOWLIST",
                "message": "holds exec with no exec_allowlist — unconstrained shell",
            }
        )
        if not sandboxed:
            out.append(
                {
                    "code": "EXEC_UNSANDBOXED",
                    "message": "unconstrained exec runs on the host (no sandbox)",
                }
            )
    return out


def _entry(m: dict, sched: dict, stats: dict) -> dict:
    agent_id = m.get("id")
    tools_allowed = m.get("tools_allowed") or []
    exec_allowlist = m.get("exec_allowlist") or []
    sandbox = m.get("sandbox")
    s = sched.get(agent_id, {})
    st = stats.get(agent_id, {})
    return {
        "agent_id": agent_id,
        "name": m.get("name"),
        "department": m.get("department"),
        "model": (m.get("model") or {}).get("primary"),
        "sandbox": sandbox,
        "delivery_mode": (m.get("delivery") or {}).get("mode"),
        "tools_allowed": tools_allowed,
        "exec_allowlist": exec_allowlist,
        "enabled": s.get("enabled"),
        "next_run_at": s.get("next_run_at").isoformat() if s.get("next_run_at") else None,
        "last_run_at": s.get("last_run_at").isoformat() if s.get("last_run_at") else None,
        "last_status": s.get("last_status"),
        "consecutive_errors": s.get("consecutive_errors"),
        "runs_7d": st.get("runs_7d", 0),
        "failures_7d": st.get("failures_7d", 0),
        "findings": _findings(tools_allowed, exec_allowlist, sandbox),
    }


@router.get("")
def list_fleet(request: Request) -> list[dict]:
    require_operator(request)
    manifests = _load_manifests()
    sched = _schedule_rows()
    stats = _run_stats()
    return [_entry(m, sched, stats) for m in manifests]


@router.get("/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict:
    require_operator(request)
    sched = _schedule_rows()
    stats = _run_stats()
    for m in _load_manifests():
        if m.get("id") == agent_id:
            return _entry(m, sched, stats)
    raise HTTPException(status_code=404, detail="unknown agent")
