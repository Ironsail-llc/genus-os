"""Tool-level post-condition checks — grade the environment, never the transcript.

A tool handler returning ``{"id": "..."}`` is the handler's *claim* that a side
effect happened. This module turns that claim into a question the environment
answers: after a side-effectful tool reports success, read the thing back and
see whether it is actually there.

Why this layer exists, when a completion contract already ships:
``completion_contract.py`` only fires when an active *session goal* exists AND
the final prose matches one of five narrow "task/goal complete" regexes. A run
that replies "✅ Payment confirmed" after writing one file to ``/tmp`` matches
none of them, and none of the CRM rows it claimed to touch were ever read back.
Post-conditions are the layer that does not care what the model said: they
compare the tool's asserted effect against durable state.

Design constraints, all of them load-bearing:

* **Verification is bookkeeping, never control flow.** Every path here is
  wrapped; an exception inside a checker is recorded as ``verify_error`` and
  the tool result is handed back untouched. A bug in this module must never
  fail an agent's real work.
* **One extra call per check**, a hard timeout, and a per-run budget
  (``MAX_CHECKS_PER_RUN``) so verification can never dominate a run.
* **Observe is side-effect-free apart from recording.** Only the ``enforce``
  rung mutates the tool result the model sees, and it does so by *adding*
  keys — the original result survives so the agent keeps whatever it needs.
* **Absent ledger degrades to a no-op.** The ``agent_run_evidence`` table
  ships in a sibling PR; until it lands, every write is caught and skipped.

Rollout: ``ROBOTHOR_TOOL_VERIFY_ENABLED`` + ``ROBOTHOR_TOOL_VERIFY_MODE``
(off → observe → alert → enforce), defaulting to observe.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.engine.feature_flags import tool_verify_mode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

GUARDRAIL_NAME = "tool_postconditions"

#: Hard ceiling on read-back calls per agent run. Verification is bookkeeping;
#: it must never become a meaningful share of a run's latency or cost.
MAX_CHECKS_PER_RUN = 20

#: Wall-clock budget for a single checker, including its one environment read.
CHECK_TIMEOUT_SECONDS = 5.0

#: Handler statuses that explicitly claim *no* new side effect (the gmail
#: duplicate-reply guard, the calendar dedup guard). Nothing was written, so
#: there is nothing to read back.
_NON_MUTATING_STATUSES = frozenset({"skipped", "deduped", "noop"})

_LEDGER_KIND = "tool_verify"
_ERROR_KIND = "verify_error"


@dataclass(frozen=True)
class VerificationOutcome:
    """The result of reading one asserted side effect back out of the world."""

    reference: str
    verified: bool
    detail: dict[str, Any] = field(default_factory=dict)
    kind: str = _LEDGER_KIND


if TYPE_CHECKING:
    Checker = Callable[
        [dict[str, Any], dict[str, Any], "ToolContext"],
        Awaitable[VerificationOutcome | None],
    ]


# ── Per-run budget ──────────────────────────────────────────────────────────
# Keyed by run_id rather than held in a ContextVar: a run's tool calls are not
# guaranteed to share one asyncio Task (sub-agent forks, tool_call meta-tool),
# and the budget must be the run's, not the task's. Bounded LRU so a long-lived
# daemon cannot accumulate one entry per run forever.

_MAX_TRACKED_RUNS = 512
_run_checks: OrderedDict[str, int] = OrderedDict()


def reset_verification_budget(run_id: str | None = None) -> None:
    """Forget the recorded check count for one run (or all runs).

    Tests call this between cases; nothing in production needs it, because the
    LRU evicts on its own.
    """
    if run_id is None:
        _run_checks.clear()
    else:
        _run_checks.pop(run_id, None)


def _consume_budget(run_id: str) -> bool:
    """Claim one check against ``run_id``'s budget. False when exhausted."""
    key = run_id or "-"
    used = _run_checks.get(key, 0)
    if used >= MAX_CHECKS_PER_RUN:
        return False
    _run_checks[key] = used + 1
    _run_checks.move_to_end(key)
    while len(_run_checks) > _MAX_TRACKED_RUNS:
        _run_checks.popitem(last=False)
    return True


# ── Evidence ledger ─────────────────────────────────────────────────────────


def _insert_evidence(
    *,
    run_id: str,
    kind: str,
    reference: str,
    verified: bool,
    detail: dict[str, Any],
) -> None:
    """Append one row to the ``agent_run_evidence`` ledger. Never raises.

    The table is created by the run-verification PR. Until that migration is
    applied the insert fails with an undefined-relation error, which is caught
    here: verification still computes and logs its verdict, it just has nowhere
    durable to file it yet.
    """
    if not run_id:
        logger.debug("tool verification: no run_id, evidence not recorded (%s)", reference)
        return
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agent_run_evidence
                    (run_id, step_id, kind, reference, verified, detail)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, None, kind, reference[:500], verified, json.dumps(detail, default=str)),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a run
        logger.debug("tool verification: evidence not recorded (%s): %s", reference, exc)


async def _record(
    ctx: ToolContext,
    tool_name: str,
    outcome: VerificationOutcome,
) -> None:
    """Persist one outcome, off the event loop, swallowing every failure."""
    detail = dict(outcome.detail)
    detail.setdefault("tool", tool_name)
    detail.setdefault("agent_id", ctx.agent_id)
    try:
        await asyncio.to_thread(
            _insert_evidence,
            run_id=ctx.run_id,
            kind=outcome.kind,
            reference=outcome.reference or tool_name,
            verified=outcome.verified,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool verification: evidence write failed: %s", exc)


# ── Environment reads ───────────────────────────────────────────────────────


async def _gws_read(argv: list[str]) -> dict[str, Any]:
    """Run one read-only ``gws`` CLI command off the event loop.

    Single seam for the Google Workspace read-backs, so tests can substitute
    the environment without a live Workspace account.
    """
    from robothor.engine.tools.handlers.gws import _run_gws

    timeout = max(1, int(CHECK_TIMEOUT_SECONDS))
    result = await asyncio.to_thread(_run_gws, argv, timeout)
    return result if isinstance(result, dict) else {"output": str(result)}


def _same(expected: Any, actual: Any) -> bool:
    """Compare a requested field value against what the row actually holds."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) is bool(actual)
    if isinstance(expected, list) or isinstance(actual, list):
        return list(expected or []) == list(actual or [])
    if expected is None:
        return actual in (None, "")
    return str(expected).strip() == str(actual if actual is not None else "").strip()


# ── Checkers ────────────────────────────────────────────────────────────────
# Each returns an outcome, or None when the call asserted no side effect worth
# reading back. Each spends at most ONE environment read.


async def _check_gmail_message(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Fetch the sent message id back from Gmail.

    A send that returns no id is recorded as unverified rather than skipped:
    "we cannot confirm this happened" is the honest record, and it is exactly
    the state an agent must not report as done.
    """
    message_id = str(result.get("id") or "").strip()
    if not message_id:
        return VerificationOutcome(
            reference="gmail:<no-id>",
            verified=False,
            detail={"reason": "send returned no message id", "result_keys": sorted(result)},
        )
    params = json.dumps({"userId": "me", "id": message_id, "format": "minimal"})
    read = await _gws_read(["gmail", "users", "messages", "get", "--params", params])
    read_id = str(read.get("id") or "")
    detail: dict[str, Any] = {"expected_id": message_id, "read_back_id": read_id}
    if "error" in read:
        detail["read_error"] = str(read["error"])[:200]
    return VerificationOutcome(
        reference=f"gmail:{message_id}",
        verified=read_id == message_id,
        detail=detail,
    )


async def _check_calendar_event(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Fetch the created event back from Calendar (a cancelled event is absent)."""
    event_id = str(result.get("id") or "").strip()
    calendar_id = str(args.get("calendar_id") or "primary")
    if not event_id:
        return VerificationOutcome(
            reference="calendar:<no-id>",
            verified=False,
            detail={"reason": "create returned no event id", "result_keys": sorted(result)},
        )
    params = json.dumps({"calendarId": calendar_id, "eventId": event_id})
    read = await _gws_read(["calendar", "events", "get", "--params", params])
    read_id = str(read.get("id") or "")
    status = str(read.get("status") or "")
    detail: dict[str, Any] = {
        "expected_id": event_id,
        "read_back_id": read_id,
        "event_status": status,
    }
    if "error" in read:
        detail["read_error"] = str(read["error"])[:200]
    return VerificationOutcome(
        reference=f"calendar:{event_id}",
        verified=read_id == event_id and status != "cancelled",
        detail=detail,
    )


def _row_id(args: dict[str, Any], result: dict[str, Any]) -> str:
    return str(result.get("id") or args.get("id") or "").strip()


async def _read_task(task_id: str, tenant_id: str) -> dict[str, Any] | None:
    from robothor.crm import dal

    return await asyncio.to_thread(dal.get_task, task_id, tenant_id)


#: crm_task fields whose requested value can be compared against the read-back
#: row without normalisation ambiguity. Timestamps and id-shaped fields are
#: deliberately excluded — their serialised forms differ harmlessly from what a
#: caller passes, and a false mismatch is worse than an unchecked field.
_TASK_COMPARABLE = ("title", "body", "status", "priority", "assignedToAgent", "resolution")


async def _check_task_created(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Read the new task row back; a title mismatch counts as unverified."""
    task_id = _row_id(args, result)
    if not task_id:
        return VerificationOutcome(
            reference="crm_task:<no-id>",
            verified=False,
            detail={"reason": "create_task returned no id"},
        )
    row = await _read_task(task_id, ctx.tenant_id)
    detail: dict[str, Any] = {"task_id": task_id, "found": row is not None}
    if row is None:
        return VerificationOutcome(reference=f"crm_task:{task_id}", verified=False, detail=detail)
    # Server-side dedup returns the pre-existing row, whose title is the one it
    # was filed under, not the one this call asked for. Existence is the whole
    # claim there; comparing titles would manufacture a false mismatch.
    title = None if result.get("deduplicated") else args.get("title")
    if title is not None and not _same(title, row.get("title")):
        detail["mismatch"] = {"title": {"requested": title, "actual": row.get("title")}}
        return VerificationOutcome(reference=f"crm_task:{task_id}", verified=False, detail=detail)
    detail["status"] = row.get("status")
    return VerificationOutcome(reference=f"crm_task:{task_id}", verified=True, detail=detail)


async def _check_task_updated(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Confirm the requested fields actually changed on the row.

    This is the motivating incident in miniature: the tool can report success
    while the row it named still reads ``TODO``.
    """
    task_id = _row_id(args, result)
    if not task_id:
        return VerificationOutcome(
            reference="crm_task:<no-id>",
            verified=False,
            detail={"reason": "update_task returned no id"},
        )
    row = await _read_task(task_id, ctx.tenant_id)
    detail: dict[str, Any] = {"task_id": task_id, "found": row is not None}
    if row is None:
        return VerificationOutcome(reference=f"crm_task:{task_id}", verified=False, detail=detail)
    # A requested value of None is never written (both dal.update_task and
    # dal.update_person skip None fields), so comparing it would manufacture a
    # mismatch for a field the caller never actually asked to change.
    mismatch = {
        key: {"requested": args[key], "actual": row.get(key)}
        for key in _TASK_COMPARABLE
        if args.get(key) is not None and not _same(args[key], row.get(key))
    }
    if mismatch:
        detail["mismatch"] = mismatch
    return VerificationOutcome(
        reference=f"crm_task:{task_id}",
        verified=not mismatch,
        detail=detail,
    )


async def _check_task_resolved(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """A resolved task must actually read back as DONE."""
    task_id = _row_id(args, result)
    if not task_id:
        return VerificationOutcome(
            reference="crm_task:<no-id>",
            verified=False,
            detail={"reason": "resolve_task returned no id"},
        )
    row = await _read_task(task_id, ctx.tenant_id)
    status = (row or {}).get("status")
    return VerificationOutcome(
        reference=f"crm_task:{task_id}",
        verified=status == "DONE",
        detail={"task_id": task_id, "found": row is not None, "status": status},
    )


#: person field -> path into the ``person_to_dict`` shape.
_PERSON_FIELDS: dict[str, tuple[str, ...]] = {
    "firstName": ("name", "firstName"),
    "lastName": ("name", "lastName"),
    "email": ("emails", "primaryEmail"),
    "phone": ("phones", "primaryPhoneNumber"),
    "jobTitle": ("jobTitle",),
    "city": ("city",),
    # Outreach opt-out. Included because an opt-out that silently did not
    # take is the failure the flag exists to prevent, and `_same` already
    # compares booleans by identity rather than string form.
    "doNotContact": ("doNotContact",),
}


def _person_field(row: dict[str, Any], path: tuple[str, ...]) -> Any:
    cursor: Any = row
    for part in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


async def _check_person_row(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Read the person row back and compare the fields the caller asked for."""
    person_id = _row_id(args, result)
    if not person_id:
        return VerificationOutcome(
            reference="crm_person:<no-id>",
            verified=False,
            detail={"reason": "person write returned no id"},
        )
    from robothor.crm import dal

    row = await asyncio.to_thread(dal.get_person, person_id, ctx.tenant_id)
    detail: dict[str, Any] = {"person_id": person_id, "found": row is not None}
    if row is None:
        return VerificationOutcome(
            reference=f"crm_person:{person_id}", verified=False, detail=detail
        )
    mismatch = {
        key: {"requested": args[key], "actual": _person_field(row, path)}
        for key, path in _PERSON_FIELDS.items()
        if args.get(key) is not None and not _same(args[key], _person_field(row, path))
    }
    if mismatch:
        detail["mismatch"] = mismatch
    return VerificationOutcome(
        reference=f"crm_person:{person_id}",
        verified=not mismatch,
        detail=detail,
    )


async def _check_notification(
    args: dict[str, Any], result: dict[str, Any], ctx: ToolContext
) -> VerificationOutcome | None:
    """Read the notification row back out of ``crm_agent_notifications``."""
    notif_id = _row_id(args, result)
    if not notif_id:
        return VerificationOutcome(
            reference="crm_notification:<no-id>",
            verified=False,
            detail={"reason": "send_notification returned no id"},
        )
    from robothor.crm import dal

    row = await asyncio.to_thread(dal.get_notification, notif_id, ctx.tenant_id)
    return VerificationOutcome(
        reference=f"crm_notification:{notif_id}",
        verified=row is not None,
        detail={"notification_id": notif_id, "found": row is not None},
    )


#: tool name -> post-condition checker. Membership is the whole contract: a
#: tool that is not here is never verified and never touched.
POST_CONDITION_CHECKS: dict[str, Checker] = {
    "gws_gmail_send": _check_gmail_message,
    "gws_gmail_reply": _check_gmail_message,
    "gws_calendar_create": _check_calendar_event,
    "create_task": _check_task_created,
    "update_task": _check_task_updated,
    "resolve_task": _check_task_resolved,
    "create_person": _check_person_row,
    "update_person": _check_person_row,
    "send_notification": _check_notification,
}


# ── Entry point ─────────────────────────────────────────────────────────────


def _enforce_message(tool_name: str, outcome: VerificationOutcome) -> str:
    return (
        f"Post-condition check FAILED for {tool_name}: the call reported success, but "
        f"reading {outcome.reference} back from the environment did not confirm it. "
        "Do NOT report this as done. Retry the action, or tell the operator plainly "
        "that it did not take effect."
    )


def _should_check(result: dict[str, Any]) -> bool:
    """Only successful, side-effect-claiming results are worth reading back."""
    if "error" in result:
        return False
    if result.get("success") is False:
        return False
    status = result.get("status")
    return not (isinstance(status, str) and status.lower() in _NON_MUTATING_STATUSES)


async def _alert(ctx: ToolContext, tool_name: str, outcome: VerificationOutcome) -> None:
    """Alert rung: observe plus a message the operator actually receives."""
    from robothor.engine.feature_flags import notify_guardrail_alert

    try:
        await asyncio.to_thread(
            notify_guardrail_alert,
            guardrail_name=GUARDRAIL_NAME,
            agent_id=ctx.agent_id,
            reason=f"{tool_name} reported success but {outcome.reference} did not read back",
            tenant_id=ctx.tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool verification: alert delivery failed: %s", exc)


async def verify_tool_result(
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """Read a tool's asserted side effect back out of the environment.

    Returns the result to hand to the model: the same object in every rung
    below ``enforce``, and in ``enforce`` a copy carrying
    ``verification_failed`` plus an actionable ``verification_message`` when
    the read-back did not confirm the write.

    Never raises. A failure anywhere in verification — a broken checker, a
    dead ledger, a timeout — is recorded and then forgotten; the agent's work
    proceeds exactly as it would have.
    """
    try:
        mode = tool_verify_mode()
        if mode == "off":
            return result
        if not isinstance(result, dict) or not _should_check(result):
            return result
        checker = POST_CONDITION_CHECKS.get(tool_name)
        if checker is None:
            return result
        if not _consume_budget(ctx.run_id):
            logger.debug(
                "tool verification: run %s hit the %d-check budget, skipping %s",
                ctx.run_id,
                MAX_CHECKS_PER_RUN,
                tool_name,
            )
            return result

        try:
            outcome = await asyncio.wait_for(
                checker(args, result, ctx), timeout=CHECK_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError
            logger.warning("tool verification: checker for %s failed: %s", tool_name, exc)
            await _record(
                ctx,
                tool_name,
                VerificationOutcome(
                    reference=tool_name,
                    verified=False,
                    detail={"error": f"{type(exc).__name__}: {exc}"[:300]},
                    kind=_ERROR_KIND,
                ),
            )
            return result

        if outcome is None:
            return result

        await _record(ctx, tool_name, outcome)
        if outcome.verified:
            return result

        logger.warning(
            "tool verification: %s reported success but %s did not read back (mode=%s) %s",
            tool_name,
            outcome.reference,
            mode,
            outcome.detail,
        )
        if mode == "alert":
            await _alert(ctx, tool_name, outcome)
        if mode == "enforce":
            enforced = dict(result)
            enforced["verification_failed"] = True
            enforced["verification_message"] = _enforce_message(tool_name, outcome)
            return enforced
        return result
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a run
        logger.warning("tool verification failed for %s: %s", tool_name, exc)
        return result
