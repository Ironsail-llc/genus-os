"""The organisation watch: what changed across the companies since the last look.

The parent tenant's main agent calls the ``org_watch`` tool on its heartbeat.
This module does the work, deterministically, so it is testable without a
model: compare the last stored snapshot with a fresh rollup
(``robothor.tenancy.rollup.tenant_rollup``), list what changed, and write the
briefing the owner reads.

Rules:

* A problem is reported once, when it appears, and once more when it clears.
  A problem present in the previous snapshot is not a change.
* A first run (no snapshot yet, or one that cannot be parsed) reports every
  problem present now. Staying silent would hide every problem that already
  existed when the watch was switched on, because later runs never repeat
  them. A healthy organisation still gets an empty briefing.
* A tenant that cannot be read keeps the problems it had in the snapshot: an
  unreadable company has not recovered, and must not be reported as having a
  "new" failure once it reads again.
* A tenant that went quiet is not told it recovered from its failures:
  nothing ran, which is not the same as nothing failing.

The snapshot lives in the parent tenant's memory block ``org_watch_snapshot``
(``robothor.memory.blocks``). Blocks are tenant-scoped, survive restarts, need
no migration, and are how other engine state is kept between runs (benchmark
and experiment state). The block is not loaded into any agent's context unless
a manifest names it in ``warmup.memory_blocks``.

Synchronous and blocking: call it off the event loop. It imports only the
rollup's shape and the memory-block helpers, never the engine's tool modules.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The memory block, in the run's own tenant, that holds the last snapshot.
SNAPSHOT_BLOCK = "org_watch_snapshot"

#: Statuses that are a matter of degree; ``error`` and ``no_activity`` are
#: reported through their own problems (``unreadable``, ``quiet``).
_GRADED = ("ok", "attention")

#: Most urgent first, for the daily summary.
_STATUS_ORDER = {"error": 0, "attention": 1, "no_activity": 2, "ok": 3}

#: The rollup's failure classes, as the owner would say them.
_REASONS = {
    "timeout": "timing out",
    "rate_limit": "hitting rate limits",
    "budget_exhausted": "running out of budget",
    "auth_error": "failing to sign in to a service",
    "connection_error": "losing its connection",
    "not_found": "asking for something that does not exist",
    "permission_error": "being refused permission",
}
_UNCLASSIFIED = "with errors the platform could not classify"


# ── snapshot ─────────────────────────────────────────────────────────────


def snapshot(rollup: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """The compact state of each tenant that the next run compares against."""
    before = (previous or {}).get("tenants") or {}
    tenants: dict[str, Any] = {}
    for entry in rollup.get("tenants") or []:
        tid = entry.get("tenant_id")
        if not tid:
            continue
        problems = _problems(entry)
        if entry.get("status") == "error":
            # Unknown is not recovered: keep what it had.
            kept = (before.get(tid) or {}).get("problems") or {}
            problems = {**{k: v for k, v in kept.items() if k != "unreadable"}, **problems}
        tenants[tid] = {
            "name": entry.get("display_name") or tid,
            "status": entry.get("status"),
            "overdue_count": int(((entry.get("tasks") or {}).get("overdue")) or 0),
            "problems": problems,
        }
    return {"as_of": rollup.get("as_of"), "tenants": tenants}


def _problems(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every problem one tenant has now, keyed so the same problem matches."""
    if entry.get("status") == "error":
        return {"unreadable": {"problem": "unreadable"}}
    out: dict[str, dict[str, Any]] = {}
    if entry.get("status") == "no_activity":
        out["quiet"] = {"problem": "quiet", "last_activity_at": entry.get("last_activity_at")}
    for agent, (count, reason) in _failing_agents(entry).items():
        out[f"failing_agent:{agent}"] = {
            "problem": "failing_agent",
            "subject": agent,
            "reason": reason,
            "count": count,
        }
    for s in entry.get("agents_with_consecutive_errors") or []:
        out[f"stuck_agent:{s['agent_id']}"] = {
            "problem": "stuck_agent",
            "subject": s["agent_id"],
            "count": int(s.get("consecutive_errors") or 0),
        }
    for s in entry.get("overdue_schedules") or []:
        out[f"late_schedule:{s['agent_id']}"] = {
            "problem": "late_schedule",
            "subject": s["agent_id"],
            "minutes": int(s.get("overdue_minutes") or 0),
        }
    for t in (entry.get("tasks") or {}).get("oldest_overdue") or []:
        out[f"overdue_task:{t.get('id')}"] = {
            "problem": "overdue_task",
            "subject": t.get("title") or "an untitled task",
            "priority": t.get("priority") or "normal",
            "overdue_days": t.get("overdueDays"),
            "age_days": t.get("ageDays"),
        }
    return out


def _failing_agents(entry: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Each failing agent's failure count and its most frequent failure class."""
    counts: dict[str, dict[str, int]] = {}
    for p in entry.get("top_failures") or []:
        agent = p.get("agent_id")
        if agent:
            by_reason = counts.setdefault(agent, {})
            reason = p.get("error_type") or "other"
            by_reason[reason] = by_reason.get(reason, 0) + int(p.get("count") or 0)
    return {agent: (sum(by.values()), max(by, key=lambda r: by[r])) for agent, by in counts.items()}


# ── diff ─────────────────────────────────────────────────────────────────


def diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """What changed between the stored snapshot and the current rollup."""
    now = snapshot(current, previous)
    before = (previous or {}).get("tenants") or {}
    changes: list[dict[str, Any]] = []
    for tid, cur in now["tenants"].items():
        changes.extend(_tenant_changes(tid, before.get(tid) or {}, cur))
    return changes


def _tenant_changes(tid: str, prev: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    head = {"tenant_id": tid, "company": cur["name"]}
    out: list[dict[str, Any]] = []
    was, now = prev.get("status"), cur["status"]
    if now in _GRADED and was in (*_GRADED, None) and was != now and (was or now != "ok"):
        out.append({**head, "change": "status", "problem": "status", "from": was, "to": now})
    old, new = prev.get("problems") or {}, cur["problems"]
    out.extend({**head, "change": "new", **new[key]} for key in new.keys() - old.keys())
    for key in old.keys() - new.keys():
        gone = old[key]
        if now == "no_activity" and gone["problem"] == "failing_agent":
            continue  # nothing ran; that is not a recovery
        out.append({**head, "change": "cleared", **gone})
    newly_listed = sum(1 for k in new.keys() - old.keys() if k.startswith("overdue_task:"))
    unlisted = cur["overdue_count"] - int(prev.get("overdue_count") or 0) - newly_listed
    if now != "error" and unlisted > 0:
        out.append({**head, "change": "new", "problem": "overdue_tasks", "count": unlisted})
    out.sort(key=_change_order)
    return out


def _change_order(change: dict[str, Any]) -> tuple[int, str]:
    rank = {"status": 0, "new": 1, "cleared": 2}[change["change"]]
    return rank, str(change.get("subject") or change["problem"])


# ── briefing ─────────────────────────────────────────────────────────────


def briefing(changes: list[dict[str, Any]], rollup: dict[str, Any], *, daily: bool) -> str:
    """The text the owner reads. Empty when there is nothing to tell."""
    as_of = _as_datetime(rollup.get("as_of")) or datetime.now(UTC)
    paragraphs = _change_paragraphs(changes, rollup, as_of)
    if not daily:
        return "\n\n".join(paragraphs)
    entries = sorted(
        rollup.get("tenants") or [], key=lambda e: _STATUS_ORDER.get(e.get("status") or "", 9)
    )
    count = len(entries)
    lines = [
        f"Daily summary of {count} {'company' if count == 1 else 'companies'}, "
        f"as of {as_of.strftime('%Y-%m-%d %H:%M')} UTC."
    ]
    lines += [_state_paragraph(e, as_of) for e in entries]
    if paragraphs:
        lines.append("Since the last check:")
        lines += paragraphs
    return "\n\n".join(lines)


def _change_paragraphs(
    changes: list[dict[str, Any]], rollup: dict[str, Any], as_of: datetime
) -> list[str]:
    order = [e.get("tenant_id") for e in rollup.get("tenants") or []]
    by_tenant: dict[str, list[dict[str, Any]]] = {}
    for c in changes:
        by_tenant.setdefault(c["tenant_id"], []).append(c)
    tids = sorted(by_tenant, key=lambda t: order.index(t) if t in order else len(order))
    return [_change_paragraph(by_tenant[t], as_of) for t in tids]


def _change_paragraph(changes: list[dict[str, Any]], as_of: datetime) -> str:
    name = changes[0]["company"]
    status = next((c for c in changes if c["change"] == "status"), None)
    if status is None:
        lead = f"{name}:"
    elif status["to"] == "attention":
        lead = f"{name} needs attention."
    else:
        lead = f"{name} is back to normal."
    said = [_change_sentence(c, as_of) for c in changes if c is not status]
    return " ".join([lead, *said])


def _change_sentence(c: dict[str, Any], as_of: datetime) -> str:
    if c["change"] == "cleared":
        return _cleared_sentence(c)
    problem = c["problem"]
    if problem == "failing_agent":
        return (
            f"The {c['subject']} agent started failing: {_n(c['count'], 'failure')} "
            f"in the last day, mostly {_REASONS.get(c['reason'], _UNCLASSIFIED)}."
        )
    if problem == "overdue_tasks":
        return f"{_n(c['count'], 'more task')} became overdue."
    if problem == "unreadable":
        return "It could not be read just now, so its state is unknown."
    if problem == "quiet":
        return f"It has gone quiet: {_quiet_since(c, as_of)}."
    return _cap(_problem_phrase(c)) + "."


def _cleared_sentence(c: dict[str, Any]) -> str:
    subject = c.get("subject")
    return {
        "failing_agent": f"The {subject} agent is no longer failing.",
        "stuck_agent": f"The {subject} agent is no longer failing run after run.",
        "late_schedule": f"The {subject} agent's schedule is on time again.",
        "overdue_task": f'The task "{subject}" is no longer overdue.',
        "unreadable": "It can be read again.",
        "quiet": "Its agents are running again.",
    }.get(c["problem"], "A problem cleared.")


def _problem_phrase(p: dict[str, Any]) -> str:
    """A problem that exists now, as a clause: for new and persisting alike."""
    problem = p["problem"]
    if problem == "failing_agent":
        return (
            f"the {p['subject']} agent failed {_n(p['count'], 'time')} in the last day, "
            f"mostly {_REASONS.get(p['reason'], _UNCLASSIFIED)}"
        )
    if problem == "stuck_agent":
        return f"the {p['subject']} agent has failed {_n(p['count'], 'run')} in a row"
    if problem == "late_schedule":
        return f"the {p['subject']} agent's scheduled run is {_minutes(p['minutes'])} late"
    if problem == "overdue_task":
        return f'the {_task_noun(p)} "{p["subject"]}" is {_overdue(p)}'
    return "something needs a look"


def _task_noun(p: dict[str, Any]) -> str:
    return "urgent task" if p.get("priority") == "urgent" else "task"


def _state_paragraph(entry: dict[str, Any], as_of: datetime) -> str:
    name = entry.get("display_name") or entry.get("tenant_id")
    status = entry.get("status")
    if status == "error":
        return f"{name} could not be read just now, so its state is unknown."
    if status == "no_activity":
        quiet = {"last_activity_at": entry.get("last_activity_at")}
        return f"{name} is quiet: {_quiet_since(quiet, as_of)}."
    problems = [p for p in _problems(entry).values() if p["problem"] != "quiet"]
    clauses = [_problem_phrase(p) for p in problems]
    listed = sum(1 for p in problems if p["problem"] == "overdue_task")
    others = int((entry.get("tasks") or {}).get("overdue") or 0) - listed
    if others > 0:
        clauses.append(f"{_n(others, 'other task')} {'is' if others == 1 else 'are'} overdue")
    if status == "attention" and clauses:
        return f"{name} needs attention: " + "; ".join(clauses) + "."
    runs = int(entry.get("runs_24h") or 0)
    return f"{name} is running normally: {_n(runs, 'run')} in the last day, none failed."


# ── run ──────────────────────────────────────────────────────────────────


def run_watch(
    home_tenant: str, rollup: dict[str, Any], *, daily: bool, store: bool
) -> dict[str, Any]:
    """Compare ``rollup`` with the stored snapshot; store the new one if ``store``."""
    previous = _load(home_tenant)
    changes = diff(previous, rollup)
    result: dict[str, Any] = {
        "changes": changes,
        "briefing": briefing(changes, rollup, daily=daily),
        "as_of": rollup.get("as_of"),
        "tenants_seen": len(rollup.get("tenants") or []),
        "stored": False,
    }
    if store:
        from robothor.memory.blocks import write_block

        written = write_block(
            SNAPSHOT_BLOCK,
            json.dumps(snapshot(rollup, previous), separators=(",", ":")),
            tenant_id=home_tenant,
        )
        result["stored"] = bool(written.get("success"))
        if not result["stored"]:
            result["store_error"] = str(written.get("error") or "the snapshot was not written")
    return result


def _load(home_tenant: str) -> dict[str, Any] | None:
    from robothor.memory.blocks import read_blocks

    block = read_blocks([SNAPSHOT_BLOCK], tenant_id=home_tenant).get(SNAPSHOT_BLOCK)
    if not block or not block.get("content"):
        return None
    try:
        loaded = json.loads(block["content"])
    except ValueError:
        logger.warning("org watch snapshot for %s unreadable; treating as a first run", home_tenant)
        return None
    return loaded if isinstance(loaded, dict) else None


# ── wording helpers ──────────────────────────────────────────────────────


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _minutes(minutes: int) -> str:
    if minutes < 120:
        return _n(minutes, "minute")
    hours = minutes // 60
    return _n(hours, "hour") if hours < 48 else _n(hours // 24, "day")


def _overdue(task: dict[str, Any]) -> str:
    days = task.get("overdue_days")
    text = "overdue (due today)" if not days else f"{_n(int(days), 'day')} overdue"
    age = task.get("age_days")
    return f"{text}, opened {_n(int(age), 'day')} ago" if age else text


def _quiet_since(problem: dict[str, Any], as_of: datetime) -> str:
    last = _as_datetime(problem.get("last_activity_at"))
    if last is None:
        return "no agent has run in the last 7 days"
    days = int((as_of - last).total_seconds() // 86400)
    return f"no agent has run for {_n(days, 'day')}"


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
