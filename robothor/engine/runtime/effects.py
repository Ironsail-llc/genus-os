"""Durable effect attempts, independent of worker memory and checkpoint versions.

The host must reserve before dispatch and finish only after a classified result.
Uncertainty is resolved by a trusted provider verifier, never by a model flag or
an absence observed while the provider may still complete the original request.
Arguments are fingerprinted, not copied into this store (they may hold secrets).
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

# Re-exported deliberately, with the `as` form mypy's no_implicit_reexport
# requires: every effect module reaches the database through THIS name, and
# the runtime tests patch it here as their single seam. Importing
# `robothor.db.connection.get_connection` directly in those modules type-checks
# and silently bypasses the patch — it broke 33 tests when I tried it.
from robothor.db.connection import get_connection as get_connection
from robothor.db.connection import tenant_scope

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.runtime.contracts import ExecutionContext


class EffectScope(Protocol):
    """What reading or resolving one record actually needs.

    `read` and `resolve` are also called from the recovery sweeps, which have
    no request and hand in a `SimpleNamespace` carrying just these two fields.
    Saying so makes that duck-typing checkable instead of a lie in the
    signature; `ExecutionContext` satisfies it structurally.
    """

    @property
    def tenant_id(self) -> str: ...

    @property
    def principal_id(self) -> str: ...


# Tools whose uncertain effects a host-owned verifier can read back and settle
# (task_recovery, note_recovery). Defined here, not in effect_recovery, because
# begin() needs it and effect_recovery imports this module.
READBACK_TOOLS: frozenset[str] = frozenset({"create_task", "create_note"})

# Tools the agent drives directly and whose result it can usually check itself:
# list the directory, read the file back, look at the page again. They are NOT
# confined to this host -- a shell command can run `curl -X POST` or `gh pr
# create`, and the browser can submit a form -- so exempting them is a trade-off,
# not a proof of safety: an uncertain call never fences a reworded call of the
# tool in a later request (its own request and identical arguments stay
# fenced), and a duplicate outside effect is accepted to keep the assistant's
# basic tools usable. Fencing them for the full window wedged an operator's
# shell for two weeks. Named here rather than taken from
# benchmark_sandbox.EXTERNAL_SIDE_EFFECT_TOOLS: that set is the benchmark's
# deny-list and deliberately includes exec and file writes.
HOST_LOCAL_TOOLS: frozenset[str] = frozenset({"exec", "execute_code", "browser", "write_file"})

# How long an uncertain effect from a tool with no verifier fences a reworded
# call of the same tool, on the same target, in a later request. Every other tool (mail, calendar,
# CRM writes, messages) cannot be read back, so a "send that again" after a
# timed-out send waits this long for reconciliation instead of sending twice.
# After it, effect_recovery.reconcile_unverifiable() releases the row as
# expired. One value for both, so the fence and the sweep cannot disagree.
UNCERTAIN_FENCE_WINDOW = "24 hours"

active_effect: ContextVar[dict[str, Any] | None] = ContextVar("active_effect", default=None)


#: The operator verb that records an unresolved effect's outcome.
RESOLVE_COMMAND = "genus effects resolve"


class EffectPendingError(ValueError):
    """A write refused BEFORE dispatch because an earlier one is unresolved.

    Nothing was sent. ``same_action`` says whether the refused call is the very
    call whose outcome is unknown (so it may already have taken effect) or a
    different call on the same target. The message names the earlier action and
    how it is cleared, so neither the model nor a person reads the refusal as
    "may have been delivered".
    """

    not_sent = True

    def __init__(
        self,
        effect_id: Any,
        *,
        record: dict[str, Any] | None = None,
        same_action: bool = False,
        verifiable: bool = False,
    ) -> None:
        self.effect_id = str(effect_id)
        self.record = dict(record or {})
        self.same_action = same_action
        self.tool = str(self.record.get("tool_name") or "")
        self.target = dict(self.record.get("target") or {})
        when = self.record.get("created_at")
        self.at = (
            when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            if isinstance(when, datetime)
            else ""
        )
        super().__init__(self._message(verifiable))

    def _message(self, verifiable: bool) -> str:
        tool = self.tool or "write"
        on = ", ".join(f"{key}={value}" for key, value in self.target.items())
        earlier = f"the earlier {tool} call" + (f" to {on}" if on else "")
        if self.at:
            earlier += f" at {self.at}"
        if self.same_action:
            head = (
                f"This call was NOT sent again: it repeats {earlier}, whose outcome is "
                f"unknown (effect {self.effect_id}) and which may already have taken effect."
            )
        else:
            head = (
                f"This write was NOT sent: {earlier} has an unknown outcome "
                f"(effect {self.effect_id}), and sending now could act on it twice."
            )
        resolve = (
            f"an operator can record its outcome with `{RESOLVE_COMMAND} {self.effect_id} "
            '--outcome applied|not-applied --reason "..."`'
        )
        if self.record.get("goal_id"):
            clear = "Its goal's reconciliation settles it."
        elif verifiable:
            clear = f"A read-back of that call clears it when it can tell; {resolve}."
        else:
            clear = f"It is released 24 hours after that call, or {resolve}."
        return f"{head} {clear}"

    def blocked_by(self) -> dict[str, Any]:
        """What the refusal names, for a tool result."""
        return {
            "effect_id": self.effect_id,
            "tool": self.tool,
            "target": self.target,
            "at": self.at,
        }


@dataclass(frozen=True)
class Verification:
    outcome: str
    settled: bool = False
    reference: str = ""
    result: dict[str, Any] | None = None


def _stored(result: Any) -> Any:
    """A provider's result as an effects row may keep it: redacted."""
    from robothor.secrets.redaction import redact_tool_result

    return redact_tool_result(result)


def fingerprint(tool_name: str, arguments: Any) -> str:
    if tool_name == "create_task" and isinstance(arguments, dict):
        # Presentation does not change the business action or its retry identity.
        arguments = {key: value for key, value in arguments.items() if key != "finalReport"}
    payload = json.dumps(
        [tool_name, arguments], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _lock(cur: Any, context: ExecutionContext) -> None:
    if context.goal_id:
        # Serialize admission against family completion/reconciliation checks.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + context.tenant_id,))
    scope = json.dumps([context.tenant_id, context.principal_id]).encode()
    key = int.from_bytes(hashlib.sha256(scope).digest()[:8], "big", signed=True)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def begin(
    context: ExecutionContext,
    run_id: str,
    agent_id: str,
    tool_name: str,
    arguments: Any,
) -> dict[str, Any]:
    """Reserve one dispatch or return an already reconciled result for this request."""
    from robothor.engine.runtime import effect_tools

    digest = fingerprint(tool_name, arguments)
    target = effect_tools.target_of(tool_name, arguments)
    key = effect_tools.target_key(tool_name, target)
    with (
        tenant_scope(context.tenant_id),
        get_connection() as conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        _lock(cur, context)
        cur.execute(
            """SELECT * FROM agent_runtime_effects WHERE tenant_id=%s AND principal_id=%s
               AND request_id=%s AND fingerprint=%s AND (state='confirmed'
                 OR state='finished' AND resolution->>'source' IN ('tool_response','operator'))
               ORDER BY created_at DESC LIMIT 1""",
            (context.tenant_id, context.principal_id, context.request_id, digest),
        )
        confirmed = cur.fetchone()
        if confirmed:
            return dict(confirmed)
        # Three fences, each with its reason:
        # 1. the identical call (same tool and arguments) while unresolved, in
        #    any request, for as long as it is unresolved: never send twice;
        # 2. any uncertain write in this request, goal or goal family: a goal's
        #    family is reconciled by the goal machinery, and within one request
        #    the agent must not route around an action it just lost track of;
        # 3. across requests, the same tool on the same TARGET (what the write
        #    acts on; effect_tools.target_of), however it is worded: a reworded
        #    "send that again" must wait. Unbounded for tools a verifier settles,
        #    never for host-local tools, else only while the uncertain row is
        #    younger than UNCERTAIN_FENCE_WINDOW. A tool may declare the whole-
        #    tool scope instead, and a row recorded before targets were has no
        #    target_key and keeps that scope too. This fence used to be the
        #    whole tool for everyone: one lost send refused every later send of
        #    that tool, to anyone, for a day.
        cur.execute(
            """SELECT id,tool_name,target,fingerprint,goal_id,created_at
               FROM agent_runtime_effects WHERE tenant_id=%s AND principal_id=%s
               AND (state IN ('prepared','dispatching','uncertain') AND fingerprint=%s
                 OR state='uncertain' AND (request_id=%s OR goal_id=%s OR budget_id=%s
                   OR (goal_id IS NULL AND budget_id IS NULL AND tool_name=%s AND NOT %s
                       AND (%s OR target_key IS NULL OR target_key=%s)
                       AND (%s OR updated_at > now() - %s::interval))))
               ORDER BY created_at LIMIT 1""",
            (
                context.tenant_id,
                context.principal_id,
                digest,
                context.request_id,
                context.goal_id,
                context.budget_id,
                tool_name,
                tool_name in HOST_LOCAL_TOOLS,
                effect_tools.fences_whole_tool(tool_name),
                key,
                effect_tools.has_verifier(tool_name),
                UNCERTAIN_FENCE_WINDOW,
            ),
        )
        pending = cur.fetchone()
        if pending:
            raise EffectPendingError(
                pending["id"],
                record=dict(pending),
                same_action=pending["fingerprint"] == digest,
                verifiable=effect_tools.has_verifier(pending["tool_name"]),
            )
        identifier = str(uuid4())
        cur.execute(
            """INSERT INTO agent_runtime_effects
               (id,tenant_id,principal_id,request_id,run_id,agent_id,goal_id,budget_id,
                tool_name,fingerprint,target,target_key,state)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared')
               RETURNING *""",
            (
                identifier,
                context.tenant_id,
                context.principal_id,
                context.request_id,
                run_id,
                agent_id,
                context.goal_id,
                context.budget_id,
                tool_name,
                digest,
                Json(effect_tools.summary(target)),
                key,
            ),
        )
        return dict(cur.fetchone())


def mark_dispatched(context: ExecutionContext, effect_id: Any, run_id: str) -> bool:
    """Only the owner of a still-prepared record may cross the dispatch boundary."""
    with tenant_scope(context.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state='dispatching',version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND request_id=%s
                 AND run_id=%s AND state='prepared'""",
            (effect_id, context.tenant_id, context.principal_id, context.request_id, run_id),
        )
        return bool(cur.rowcount == 1)


def finish(context: ExecutionContext, effect_id: Any, run_id: str, *, uncertain: bool) -> bool:
    """A late worker cannot overwrite a recovery verdict or another worker's record."""
    with tenant_scope(context.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=%s,version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND request_id=%s
                 AND run_id=%s AND state IN ('prepared','dispatching')""",
            (
                "uncertain" if uncertain else "finished",
                effect_id,
                context.tenant_id,
                context.principal_id,
                context.request_id,
                run_id,
            ),
        )
        return bool(cur.rowcount == 1)


def abandon_run(context: ExecutionContext, run_id: str) -> int:
    """The host observed worker loss: surviving dispatch intents now require readback."""
    with tenant_scope(context.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=CASE WHEN state='prepared' THEN 'not_applied' ELSE 'uncertain' END,
               version=version+1,updated_at=now()
               WHERE tenant_id=%s AND principal_id=%s AND run_id=%s AND state IN ('prepared','dispatching')""",
            (context.tenant_id, context.principal_id, run_id),
        )
        return int(cur.rowcount)


def read(context: EffectScope, effect_id: Any) -> dict[str, Any] | None:
    with (
        tenant_scope(context.tenant_id),
        get_connection() as conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        cur.execute(
            "SELECT * FROM agent_runtime_effects WHERE id=%s AND tenant_id=%s AND principal_id=%s",
            (effect_id, context.tenant_id, context.principal_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def resolve(
    context: EffectScope, effect_id: Any, verifier: Callable[[dict[str, Any]], Verification]
) -> bool:
    """Invoke a host-owned provider verifier outside the database transaction.

    A not-applied verdict must be definitive: an eventually consistent missing
    resource, or an in-flight request, does not establish that no effect can occur.
    This is a host extension interface, not a tool or model-supplied callback.
    """
    record = read(context, effect_id)
    if not record or record["state"] != "uncertain":
        return False
    verdict = verifier(record)
    if not isinstance(verdict, Verification) or verdict.outcome not in {
        "applied",
        "not_applied",
        "unknown",
    }:
        raise ValueError("invalid provider verification")
    if verdict.outcome == "unknown" or verdict.settled is not True:
        return False
    if not verdict.reference or not isinstance(verdict.reference, str):
        raise ValueError("provider evidence reference required")
    if verdict.outcome == "applied" and (
        not isinstance(verdict.result, dict) or verdict.result.get("error")
    ):
        raise ValueError("verified result required")
    with tenant_scope(context.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=%s,resolution=%s,version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND version=%s AND state='uncertain'""",
            (
                "confirmed" if verdict.outcome == "applied" else "not_applied",
                Json({"reference": verdict.reference, "result": _stored(verdict.result)}),
                effect_id,
                context.tenant_id,
                context.principal_id,
                record["version"],
            ),
        )
        return bool(cur.rowcount == 1)


def attest(context: EffectScope, effect_id: Any, *, actor: str, note: str) -> bool:
    """Record an authorized reconciliation without manufacturing provider evidence.

    Callers must establish operator authority. Active dispatches cannot be
    attested: their workers must first stop and leave an uncertain receipt.
    """
    return _release(context, effect_id, source="reconciled", actor=actor, note=note)


def expire(context: EffectScope, effect_id: Any, *, actor: str, note: str) -> bool:
    """Release a goal-less uncertain effect whose fence window lapsed unverified.

    The system's housekeeping, not an operator decision: recorded with source
    ``expired`` so no receipt can present it as a human reconciliation.
    """
    return _release(context, effect_id, source="expired", actor=actor, note=note)


def _release(context: EffectScope, effect_id: Any, *, source: str, actor: str, note: str) -> bool:
    if not actor.strip() or not note.strip():
        raise ValueError("reconciliation requires an actor and an audit note")
    with tenant_scope(context.tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state='finished', resolution=%s,
               version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND goal_id IS NULL AND state='uncertain'""",
            (
                Json({"source": source, "verified": False, "actor": actor, "note": note}),
                effect_id,
                context.tenant_id,
                context.principal_id,
            ),
        )
        return bool(cur.rowcount == 1)


def unresolved(tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """One tenant's unresolved effects, oldest first, for an operator. Reads only."""
    with (
        tenant_scope(tenant_id),
        get_connection() as conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        cur.execute(
            """SELECT id,principal_id,request_id,goal_id,tool_name,target,state,
                      created_at,updated_at
               FROM agent_runtime_effects
               WHERE tenant_id=%s AND state IN ('prepared','dispatching','uncertain')
               ORDER BY created_at,id LIMIT %s""",
            (tenant_id, max(1, min(int(limit), 1000))),
        )
        return [dict(row) for row in cur.fetchall()]


OPERATOR_OUTCOMES = frozenset({"applied", "not_applied"})


def operator_resolve(
    tenant_id: str, effect_id: Any, *, outcome: str, actor: str, reason: str
) -> bool:
    """Record an operator's finding about an uncertain effect, with the reason.

    ``applied``: the operator saw it take effect; the row is finished, and the
    identical call in its own request is answered from it, never sent again.
    ``not_applied``: the operator established it did not; a new attempt may go.
    Neither is provider evidence (``verified`` stays false). Only an uncertain,
    goal-less effect of this tenant can be resolved this way: an in-flight
    dispatch must stop first, and a goal's family is the goal's to reconcile.
    """
    if outcome not in OPERATOR_OUTCOMES:
        raise ValueError("outcome must be 'applied' or 'not_applied'")
    if not str(actor).strip() or not str(reason).strip():
        raise ValueError("an operator resolution requires an actor and a reason")
    resolution = {
        "source": "operator",
        "outcome": outcome,
        "verified": False,
        "actor": actor,
        "note": reason,
        "result": {"outcome": outcome, "resolved_by": "operator", "note": reason},
    }
    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=%s,resolution=%s,
               version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND goal_id IS NULL AND state='uncertain'""",
            (
                "finished" if outcome == "applied" else "not_applied",
                Json(resolution),
                str(effect_id),
                tenant_id,
            ),
        )
        return bool(cur.rowcount == 1)
