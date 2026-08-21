"""
Session Warmth — pre-loads context so agents start warm, not cold.

Builds a preamble string from:
1. Session history (last run status, duration, errors)
2. Memory blocks (operational_findings, contacts_summary, etc.)
3. Context files (status files agents would otherwise waste tool calls reading)
4. Peer agent status (what related agents did recently)

Every section wrapped in try/except — never crashes, silently degrades.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.identity import enrich_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.engine.models import AgentConfig
    from robothor.identity import IdentityContext
    from robothor.identity.scope import DataScope

logger = logging.getLogger(__name__)

MAX_WARMTH_CHARS = 4000
MAX_BLOCK_CHARS = 800
MAX_FILE_CHARS = 600

# ── Unread alert digest ───────────────────────────────────────────
# ``robothor/engine/alerts.py`` pages Telegram only for level='critical';
# 'warning'/'info' become ``alert_digest`` rows in ``crm_agent_notifications``
# addressed to the operator-facing agent. Nothing used to read that table, so
# every warning-level alert the platform ever raised went to a write-only
# surface. These constants bound the reader below.

#: Agent the engine addresses operator-facing notifications to. Must match
#: ``alerts.py::_write_notification``'s ``to_agent`` and the digest is only
#: surfaced to this agent — workers do not get the operator's inbox.
OPERATOR_INBOX_AGENT_ID = "main"

#: Notification types written by the alert router (see ``alerts.py``).
#: ``alert_fallback`` is a *critical* page that failed to deliver, so it is
#: surfaced alongside the digest rather than lost.
ALERT_DIGEST_TYPES: tuple[str, ...] = ("alert_digest", "alert_fallback")

ALERT_SECTION_HEADER = "--- UNREAD ALERTS"
MAX_ALERT_ROWS = 8
MAX_ALERT_SECTION_CHARS = 900
MAX_ALERT_SUBJECT_CHARS = 120

# ── Warmup kind (cron | interactive) ──────────────────────────────
# Runner sets this around the warmup call so hooks can discriminate
# between scheduled heartbeat runs and interactive chat turns. ContextVars
# do not auto-propagate to executors, so runner sets it *inside* the
# executor closure via `set_warmup_kind`.

_CURRENT_WARMUP_KIND: ContextVar[str | None] = ContextVar("robothor_warmup_kind", default=None)


@contextmanager
def set_warmup_kind(kind: str | None) -> Iterator[None]:
    token = _CURRENT_WARMUP_KIND.set(kind)
    try:
        yield
    finally:
        _CURRENT_WARMUP_KIND.reset(token)


def current_warmup_kind() -> str | None:
    return _CURRENT_WARMUP_KIND.get()


# ── Dynamic context hooks ─────────────────────────────────────────
# Callables that return optional context strings. Called during warmup
# preamble construction. Each hook has a 100ms timeout.

_CONTEXT_HOOKS: list[Callable[[], str | None]] = []
_AGENT_CONTEXT_HOOKS: list[Callable[[AgentConfig], str | None]] = []


def register_context_hook(fn: Callable[[], str | None]) -> None:
    """Register a dynamic context hook for warmup preambles."""
    _CONTEXT_HOOKS.append(fn)


def register_agent_context_hook(fn: Callable[[AgentConfig], str | None]) -> None:
    """Register an agent-aware context hook (receives AgentConfig)."""
    _AGENT_CONTEXT_HOOKS.append(fn)


def _run_context_hooks() -> str:
    """Run all context hooks, collecting results within 100ms timeout each."""
    import time

    results: list[str] = []
    for hook in _CONTEXT_HOOKS:
        try:
            start = time.monotonic()
            result = hook()
            elapsed = time.monotonic() - start
            if elapsed > 0.1:
                logger.debug("Context hook %s took %.0fms (>100ms)", hook.__name__, elapsed * 1000)
            if result:
                results.append(result)
        except Exception as e:
            logger.debug("Context hook %s failed: %s", hook.__name__, e)

    if not results:
        return ""
    return "--- SITUATIONAL CONTEXT ---\n" + "\n".join(results)


def build_warmth_preamble(
    config: AgentConfig,
    workspace: Path,
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[str, dict[str, float]]:
    """Build a warmth preamble string for an agent run.

    Returns (preamble, section_timings) where preamble is up to
    MAX_WARMTH_CHARS of pre-loaded context (empty string if none) and
    section_timings is a dict mapping section name -> elapsed seconds.
    The caller can record per-section warmup_phase steps using this data.
    """
    sections: list[str] = []
    total_start = time.monotonic()
    # Section timings for stall diagnosis — heartbeat runs have been timing
    # out before the first LLM call, and warmup is the biggest blocking
    # slab in init. Log anything > 500ms per section, and total > 5s.
    _section_timings: dict[str, float] = {}

    def _run_section(name: str, fn: Callable[[], str | None]) -> None:
        start = time.monotonic()
        try:
            result = fn()
        except Exception as e:
            logger.debug("Warmup %s failed for %s: %s", name, config.id, e)
            result = None
        elapsed = time.monotonic() - start
        _section_timings[name] = elapsed
        if elapsed > 0.5:
            logger.info("warmup %s: section=%s ms=%d", config.id, name, int(elapsed * 1000))
        if result:
            sections.append(result)

    # Unread alert digest first: it is the only section carrying alerts that
    # deliberately did not page, and it must survive MAX_WARMTH_CHARS.
    _surfaced_alert_ids: list[str] = []

    def _unread_alerts() -> str | None:
        if config.id != OPERATOR_INBOX_AGENT_ID:
            return None
        text, ids = _build_unread_alerts_section(tenant_id)
        _surfaced_alert_ids.extend(ids)
        return text or None

    _run_section("unread_alerts", _unread_alerts)
    _run_section("history", lambda: _build_history_section(config.id))
    _run_section(
        "memory_blocks",
        lambda: _build_memory_blocks_section(config.warmup_memory_blocks, tenant_id=tenant_id),
    )
    _run_section(
        "context_files",
        lambda: _build_context_files_section(config.warmup_context_files, workspace),
    )
    _run_section("peers", lambda: _build_peer_section(config.warmup_peer_agents))
    _run_section("context_hooks", _run_context_hooks)

    def _breadcrumbs() -> str | None:
        from robothor.memory.breadcrumbs import (
            format_breadcrumbs_for_warmup,
            load_recent_breadcrumbs,
        )

        breadcrumbs = load_recent_breadcrumbs(config.id, limit=5, tenant_id=tenant_id)
        return format_breadcrumbs_for_warmup(breadcrumbs)

    _run_section("breadcrumbs", _breadcrumbs)

    def _preferences() -> str | None:
        from robothor.memory.preferences import get_stale_preferences

        stale = get_stale_preferences(tenant_id=tenant_id)
        if not stale:
            return None
        lines = ["# Preferences flagged as possibly stale (verify with operator)"]
        lines.extend(f"- {p.get('preference', '?')}" for p in stale[:5])
        return "\n".join(lines)

    _run_section("preferences", _preferences)
    _run_section("agent_hooks", lambda: _run_agent_context_hooks(config))

    def _agent_goal() -> str | None:
        # Late import: session_goal + goals import robothor.crm.dal which
        # transitively touches DB drivers. Keep warmup lightweight when unused.
        from robothor.engine.session_goal import build_agent_goal_context

        ctx = build_agent_goal_context(
            tenant_id=tenant_id,
            agent_id=config.id,
            manifest_path=getattr(config, "manifest_path", None),
        )
        return ctx or None

    _run_section("agent_goal", _agent_goal)

    def _goal_recall() -> str | None:
        # R2: cron/scheduled runs otherwise start with no query-relevant recall
        # (only interactive warmup extracts entities). Seed entity recall from
        # the agent's goal so a heartbeat run recalls facts about its objective's
        # entities instead of re-deriving them. Reuses the interactive helper.
        from robothor.engine.feature_flags import cron_warmup_recall_enabled

        if not cron_warmup_recall_enabled():
            return None
        from robothor.engine.session_goal import build_agent_goal_context

        goal_text = build_agent_goal_context(
            tenant_id=tenant_id,
            agent_id=config.id,
            manifest_path=getattr(config, "manifest_path", None),
        )
        if not goal_text:
            return None
        return _build_entity_context(goal_text, tenant_id=tenant_id) or None

    _run_section("goal_recall", _goal_recall)

    def _active_intents() -> str | None:
        # Prospective/intent memory (RIP 14) — what the operator is working
        # toward, so the heartbeat can advance standing objectives.
        from robothor.engine.feature_flags import is_rip_enabled

        if not is_rip_enabled(14):
            return None
        from robothor.memory.intents import build_active_intents_context

        return build_active_intents_context(tenant_id)

    _run_section("active_intents", _active_intents)

    total_elapsed = time.monotonic() - total_start
    if total_elapsed > 5.0:
        breakdown = " ".join(
            f"{k}={int(v * 1000)}" for k, v in _section_timings.items() if v > 0.05
        )
        logger.warning(
            "warmup %s: total_ms=%d breakdown=%s",
            config.id,
            int(total_elapsed * 1000),
            breakdown,
        )

    if not sections:
        return "", _section_timings

    preamble = "\n\n".join(sections)
    if len(preamble) > MAX_WARMTH_CHARS:
        preamble = preamble[:MAX_WARMTH_CHARS] + "\n[warmup truncated]"

    _ack_surfaced_alerts(_surfaced_alert_ids, preamble, tenant_id)

    return preamble, _section_timings


def _build_history_section(agent_id: str) -> str:
    """Build session history from agent_schedules."""
    from robothor.engine.tracking import get_schedule

    schedule = get_schedule(agent_id)
    if not schedule:
        return ""

    lines = ["--- SESSION HISTORY ---"]

    last_status = schedule.get("last_status")
    if last_status:
        lines.append(f"Last run: {last_status}")

    last_duration = schedule.get("last_duration_ms")
    if last_duration is not None:
        lines.append(f"Duration: {last_duration}ms")

    last_run_at = schedule.get("last_run_at")
    if last_run_at:
        if isinstance(last_run_at, datetime):
            now = datetime.now(UTC)
            delta = (
                now - last_run_at.replace(tzinfo=UTC)
                if last_run_at.tzinfo is None
                else now - last_run_at
            )
            hours = delta.total_seconds() / 3600
            lines.append(f"Hours since last run: {hours:.1f}")
        else:
            lines.append(f"Last run at: {last_run_at}")

    consecutive_errors = schedule.get("consecutive_errors", 0)
    if consecutive_errors and consecutive_errors > 0:
        lines.append(f"WARNING: {consecutive_errors} consecutive errors")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_memory_blocks_section(block_names: list[str], tenant_id: str = DEFAULT_TENANT) -> str:
    """Read memory blocks and format them, flagging stale ones."""
    if not block_names:
        return ""

    from robothor.memory.blocks import read_block

    lines = ["--- MEMORY BLOCKS ---"]
    for name in block_names:
        try:
            result = read_block(name, tenant_id=tenant_id)
            content = (
                result.get("content", "")
                if isinstance(result, dict)
                else str(result)
                if result
                else ""
            )
            if content:
                # Check staleness — flag blocks older than 24h
                stale_tag = ""
                last_written = result.get("last_written_at") if isinstance(result, dict) else None
                if last_written:
                    try:
                        from datetime import datetime as _dt

                        written_dt = _dt.fromisoformat(last_written)
                        if written_dt.tzinfo is None:
                            written_dt = written_dt.replace(tzinfo=UTC)
                        age_hours = (datetime.now(UTC) - written_dt).total_seconds() / 3600
                        if age_hours > 24:
                            stale_tag = f" [STALE — {age_hours:.0f}h old]"
                    except (ValueError, TypeError):
                        pass

                truncated = content[:MAX_BLOCK_CHARS]
                if len(content) > MAX_BLOCK_CHARS:
                    truncated += "..."
                lines.append(f"[{name}]{stale_tag}\n{truncated}")
        except Exception as e:
            logger.debug("Failed to read memory block %s: %s", name, e)

    return "\n".join(lines) if len(lines) > 1 else ""


# Memory blocks that describe the OPERATOR (not the agent) — under
# ``ROBOTHOR_DATA_SCOPING=enforce`` a restricted (non-privileged) identity
# must not have these pre-loaded into its prompt. ``persona`` is excluded —
# it describes the AGENT, not the operator, so every caller keeps it.
_OPERATOR_PERSONAL_BLOCKS: tuple[str, ...] = ("user_profile", "user_model", "working_context")


def _count_operator_blocks_with_content(
    block_names: tuple[str, ...] = _OPERATOR_PERSONAL_BLOCKS, tenant_id: str = DEFAULT_TENANT
) -> int:
    """How many of ``block_names`` currently have content.

    Used only for observe-mode would-drop counting (Task 5 warmup fix) —
    counts blocks that enforce mode would have excluded, without altering
    output. Mirrors the content-extraction logic in
    ``_build_memory_blocks_section``.
    """
    from robothor.memory.blocks import read_block

    count = 0
    for name in block_names:
        try:
            result = read_block(name, tenant_id=tenant_id)
            content = (
                result.get("content", "")
                if isinstance(result, dict)
                else str(result)
                if result
                else ""
            )
            if content:
                count += 1
        except Exception as e:
            logger.debug("Observe-scope block count failed for %s: %s", name, e)
    return count


def _build_context_files_section(file_paths: list[str], workspace: Path) -> str:
    """Read context files (status files etc.) and format them."""
    if not file_paths:
        return ""

    lines = ["--- CONTEXT FILES ---"]
    for rel_path in file_paths:
        try:
            full_path = workspace / rel_path
            if not full_path.exists():
                continue
            content = full_path.read_text()
            if not content.strip():
                continue
            truncated = content[:MAX_FILE_CHARS]
            if len(content) > MAX_FILE_CHARS:
                truncated += "..."
            age_hours = (time.time() - full_path.stat().st_mtime) / 3600
            age_label = f" (stale — {age_hours:.0f}h ago)" if age_hours > 4 else ""
            lines.append(f"[{rel_path}]{age_label}\n{truncated}")
        except Exception as e:
            logger.debug("Failed to read context file %s: %s", rel_path, e)

    return "\n".join(lines) if len(lines) > 1 else ""


def _alert_age_label(created_at: Any) -> str:
    """Render a compact age label ('2h ago') for a notification timestamp."""
    try:
        if isinstance(created_at, str):
            created = datetime.fromisoformat(created_at)
        elif isinstance(created_at, datetime):
            created = created_at
        else:
            return "?"
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        minutes = (datetime.now(UTC) - created).total_seconds() / 60
    except (ValueError, TypeError):
        return "?"
    if minutes < 60:
        return f"{max(int(minutes), 0)}m ago"
    if minutes < 60 * 48:
        return f"{int(minutes // 60)}h ago"
    return f"{int(minutes // 1440)}d ago"


def _fetch_unread_alerts(tenant_id: str, limit: int = MAX_ALERT_ROWS) -> list[dict[str, Any]]:
    """Read unread ``alert_digest``/``alert_fallback`` rows for the operator agent.

    Returns up to ``limit + 1`` rows (newest first) — the extra row is how the
    caller detects "there are more pending" without a second count query.
    """
    from robothor.crm.dal import get_agent_inbox

    rows: list[dict[str, Any]] = []
    for notification_type in ALERT_DIGEST_TYPES:
        rows.extend(
            get_agent_inbox(
                agent_id=OPERATOR_INBOX_AGENT_ID,
                unread_only=True,
                type_filter=notification_type,
                limit=limit + 1,
                tenant_id=tenant_id,
            )
        )
    rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
    return rows[: limit + 1]


def _build_unread_alerts_section(
    tenant_id: str,
    limit: int = MAX_ALERT_ROWS,
    max_chars: int = MAX_ALERT_SECTION_CHARS,
) -> tuple[str, list[str]]:
    """Render the operator's unread alert digest, plus the ids actually rendered.

    Every ``warning``/``info`` alert is written to ``crm_agent_notifications``
    instead of paging Telegram. Without this section a row reaches nobody.

    Args:
        tenant_id: Tenant whose operator inbox to read.
        limit: Maximum digest rows to render.
        max_chars: Hard character cap for the whole section, header included.

    Returns:
        ``(section_text, rendered_ids)``. ``("", [])`` when there is nothing
        unread — an empty inbox must add no noise to the preamble. Only ids
        whose line survived the caps appear in ``rendered_ids``, so the caller
        never acknowledges a row it did not actually show.
    """
    rows = _fetch_unread_alerts(tenant_id, limit=limit)
    if not rows:
        return "", []

    more_pending = len(rows) > limit
    rows = rows[:limit]

    header = f"{ALERT_SECTION_HEADER} ({len(rows)}) ---"
    hint = (
        "Warning/info alerts that did NOT page. Act on them, then clear each "
        "with ack_notification(notificationId=...)."
    )
    lines = [header, hint]
    chars_used = len(header) + len(hint) + 1
    rendered_ids: list[str] = []

    for row in rows:
        row_id = str(row.get("id") or "")
        if not row_id:
            continue
        subject = str(row.get("subject") or "(no subject)").replace("\n", " ")
        if len(subject) > MAX_ALERT_SUBJECT_CHARS:
            subject = subject[:MAX_ALERT_SUBJECT_CHARS] + "…"
        line = f"• {_alert_age_label(row.get('createdAt'))} — {subject} — id={row_id}"
        if chars_used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        chars_used += len(line) + 1
        rendered_ids.append(row_id)

    if not rendered_ids:
        return "", []

    hidden = len(rows) - len(rendered_ids)
    if hidden or more_pending:
        note = f"({hidden}+ older alerts not shown — read them with get_inbox)"
        if chars_used + len(note) + 1 <= max_chars:
            lines.append(note)

    return "\n".join(lines), rendered_ids


def _ack_surfaced_alerts(ids: list[str], delivered: str, tenant_id: str) -> int:
    """Acknowledge digest rows whose text verifiably reached the delivered preamble.

    The preamble is hard-truncated at ``MAX_WARMTH_CHARS`` *after* the sections
    are assembled, so "the section was built" is not "the operator saw it".
    This mirrors ``alerts.py``'s ``delivered = bool(sent)``: check, don't
    assume — the Telegram arity bug hid behind exactly that assumption while
    432+ alerts went nowhere. A row whose id is absent from ``delivered`` stays
    unread and is surfaced again on the next run.

    Args:
        ids: Notification ids the section rendered.
        delivered: The final preamble text, post-truncation.
        tenant_id: Tenant the notifications belong to.

    Returns:
        Number of rows acknowledged. Never raises.
    """
    if not ids or not delivered:
        return 0
    acked = 0
    try:
        from robothor.crm.dal import acknowledge_notification

        for row_id in ids:
            if row_id not in delivered:
                logger.debug("Alert %s truncated out of preamble; leaving unread", row_id)
                continue
            try:
                if acknowledge_notification(row_id, tenant_id=tenant_id):
                    acked += 1
            except Exception as e:
                logger.debug("Acking surfaced alert %s failed: %s", row_id, e)
    except Exception as e:
        logger.debug("Alert acknowledgement pass failed: %s", e)
    return acked


def _open_tasks_section(
    tenant_id: str,
    limit: int = 10,
    scope: DataScope | None = None,
    observe_scope_obj: DataScope | None = None,
    user_id: str | None = None,
) -> str:
    """Render the top open tasks grouped by assigned agent.

    For main's Telegram warmup — lets the supervisor answer 'what's open?'
    without spinning tool calls.

    ``scope`` (final-review Fix 2 / Task 5 follow-up): threaded straight into
    ``list_tasks`` — under enforce, a restricted identity's FIRST Telegram
    message otherwise pre-loaded the tenant's whole open-task queue with no
    prior tool call to filter it through. ``observe_scope_obj`` is
    dry-run-only would-drop counting, same as ``_build_entity_context``.
    """
    try:
        from robothor.crm.dal import list_tasks
        from robothor.identity.scope import log_would_drop, rows_dropped_by_scope

        rows = list_tasks(
            tenant_id=tenant_id,
            exclude_resolved=True,
            limit=limit,
            scope=scope,
        )

        if observe_scope_obj is not None and rows:
            # list_tasks() rows are task_to_dict() output (camelCase keys),
            # not raw DB columns — person_key must match that shape.
            _dropped = rows_dropped_by_scope(rows, observe_scope_obj, person_key="personId")
            log_would_drop(
                tool_name="warmup:open_tasks",
                user_id=user_id,
                scope=observe_scope_obj,
                dropped=_dropped,
                table="crm_tasks",
            )

        if not rows:
            return "--- OPEN TASKS ---\nNothing open."
        grouped: dict[str, list[dict[str, Any]]] = {}
        for t in rows:
            key = t.get("assigned_to_agent") or "unassigned"
            grouped.setdefault(key, []).append(t)
        lines = ["--- OPEN TASKS ---"]
        for agent, tasks in sorted(grouped.items()):
            lines.append(f"[{agent}]")
            for t in tasks[:5]:
                obj = t.get("objective") or ""
                obj_part = f" — {obj}" if obj else ""
                short_id = str(t.get("id") or "")[:8]
                lines.append(
                    f"  • {t.get('title', '(no title)')} "
                    f"({t.get('status', '?')}{obj_part}) [{short_id}]"
                )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Open-tasks section failed: %s", e)
        return ""


def _recent_fleet_surfaces(
    tenant_id: str,
    hours: int = 6,
    limit: int = 6,
    scope: DataScope | None = None,
    observe_scope_obj: DataScope | None = None,
    user_id: str | None = None,
) -> str:
    """Pull recent fleet agent deliveries from the channel bus (dual-writes
    into main's session with origin='channel_bus'). Gives main awareness of
    what other agents posted to the operator's Telegram channel in the last
    few hours.

    ``scope`` (final-review Fix 2 / Task 5 follow-up): fleet surfaces are
    operator-oriented (what other agents told the operator on their own
    Telegram channel) — they carry no ``person_id`` to filter row-by-row, so
    under enforce a restricted identity skips the section entirely rather
    than the "own data + shared" row rule used elsewhere. ``observe_scope_obj``
    is dry-run-only would-drop counting: every row surfaced counts as a drop
    since the whole section would have been skipped under enforce.
    """
    if scope is not None and scope.restricted:
        return ""
    try:
        from robothor.db import get_connection

        sql = """
            SELECT
                cm.created_at,
                cm.message->>'author_agent_id' AS author,
                COALESCE(cm.message->>'content', '') AS content
            FROM chat_messages cm
            JOIN chat_sessions cs ON cs.id = cm.session_id
            WHERE cs.tenant_id = %s
              AND cm.message->>'origin' = 'channel_bus'
              AND cm.message->>'author_agent_id' IS NOT NULL
              AND cm.message->>'author_agent_id' != 'main'
              AND cm.created_at > NOW() - (%s || ' hours')::interval
            ORDER BY cm.created_at DESC
            LIMIT %s
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (tenant_id, str(hours), limit))
                rows = cur.fetchall()
        if not rows:
            return ""

        if observe_scope_obj is not None:
            from robothor.identity.scope import log_would_drop

            log_would_drop(
                tool_name="warmup:fleet_surfaces",
                user_id=user_id,
                scope=observe_scope_obj,
                dropped=len(rows),
                table="chat_messages",
            )

        lines = [f"--- RECENT FLEET SURFACES (last {hours}h) ---"]
        for created_at, author, content in rows:
            ts = created_at.strftime("%H:%M") if created_at else "?"
            snippet = (content or "").strip().split("\n", 1)[0][:140]
            lines.append(f"[@{author} {ts}] {snippet}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Fleet surfaces section failed: %s", e)
        return ""


def build_interactive_preamble(
    agent_id: str,
    user_message: str = "",
    include_blocks: bool = True,
    tenant_id: str = DEFAULT_TENANT,
    extra_memory_blocks: list[str] | None = None,
    sender_name: str = "",
    identity: IdentityContext | None = None,
) -> str:
    """Build a lightweight warmup preamble for interactive (Telegram) sessions.

    Injects core memory blocks (persona, user_profile, working_context) and
    optionally pulls relevant facts based on entities mentioned in the user's message.

    Args:
        agent_id: The agent ID (for history lookup).
        user_message: The user's message (for entity-aware context).
        include_blocks: If True, inject core memory blocks (persona, user_profile,
            working_context). Set to False for ongoing sessions where blocks are
            already in conversation history.
        sender_name: Display name of the current user. When set (and ``identity``
            is not), injects a legacy identity section and excludes the name
            from entity context search. Kept for back-compat.
        identity: Unified identity context (see ``robothor.identity``). When
            set, takes precedence over ``sender_name`` — the agent gets the
            full ``--- CURRENT USER ---`` block (enriched with CRM/memory-graph
            context when available) instead of the bare-name legacy text, and
            ``identity.display_name`` is what gets excluded from entity search.

    Returns:
        Warmup preamble string, or empty string if nothing to inject.
    """
    sections: list[str] = []
    exclude_name = sender_name

    # Unread alert digest — FIRST, deliberately. These are the alerts that did
    # not page, and the preamble is hard-truncated at MAX_WARMTH_CHARS; memory
    # blocks and entity recall alone can exhaust that budget, so anything added
    # after them is not reliably delivered. Capped at MAX_ALERT_SECTION_CHARS.
    _surfaced_alert_ids: list[str] = []
    if agent_id == OPERATOR_INBOX_AGENT_ID:
        try:
            alerts_section, _surfaced_alert_ids = _build_unread_alerts_section(tenant_id)
            if alerts_section:
                sections.append(alerts_section)
        except Exception as e:
            logger.debug("Interactive warmup unread-alerts section failed: %s", e)

    # "Own data + shared" row scoping (Task 5 / final-review Fix 1) — a
    # restricted (non-privileged) identity's FIRST message has no prior tool
    # call to filter through, so this pipeline is the one place scoping must
    # be applied at build time rather than at the DAL layer. Off mode /
    # identity=None / privileged identity all resolve to ``None`` here, so
    # the rest of this function is byte-identical to pre-Task-5 behavior in
    # those cases — see ``robothor.identity.scope``.
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import log_would_drop, observe_scope, scope_for_query

    _scoping_mode = data_scoping_mode()
    _enforce_scope: DataScope | None = scope_for_query(_scoping_mode, identity)
    _observe_scope: DataScope | None = observe_scope(_scoping_mode, identity)
    _scope_user_id = (
        (identity.user_account_id or identity.tenant_user_id or identity.identifier)
        if identity is not None
        else None
    )

    # Current-user identity — tell the agent exactly who it's talking to.
    # `identity` (the unified context) always wins over the legacy
    # `sender_name` string when both are present.
    if identity is not None:
        try:
            enriched = enrich_identity(identity)
        except Exception as e:
            logger.debug("Interactive warmup identity enrichment failed: %s", e)
            enriched = None
        try:
            sections.append(identity.prompt_block(enriched))
        except Exception as e:
            logger.debug("Interactive warmup identity prompt_block failed: %s", e)
        exclude_name = identity.display_name or sender_name
    elif sender_name:
        sections.append(
            f"--- CURRENT USER ---\n"
            f"You are speaking with {sender_name}. Address them by this name.\n"
            f"Do not confuse them with other people who may share the same name."
        )

    # Core memory blocks — only for new sessions (no prior history)
    if include_blocks:
        core_blocks = ["persona", "user_profile", "user_model", "working_context"]
        # Also include agent-configured warmup blocks (e.g. devops_latest_report)
        if extra_memory_blocks:
            core_blocks = list(dict.fromkeys(core_blocks + extra_memory_blocks))

        # user_profile/user_model/working_context describe the OPERATOR —
        # a restricted identity under enforce must not have them pre-loaded.
        # persona describes the AGENT and always stays; the identity's own
        # CRM/memory-graph enrichment (above, via enrich_identity) is what a
        # restricted caller gets instead.
        if _enforce_scope is not None:
            core_blocks = [b for b in core_blocks if b not in _OPERATOR_PERSONAL_BLOCKS]

        try:
            blocks_section = _build_memory_blocks_section(core_blocks, tenant_id=tenant_id)
            if blocks_section:
                sections.append(blocks_section)
        except Exception as e:
            logger.debug("Interactive warmup blocks failed: %s", e)

        if _observe_scope is not None:
            try:
                _dropped_blocks = _count_operator_blocks_with_content(tenant_id=tenant_id)
                if _dropped_blocks:
                    log_would_drop(
                        tool_name="warmup:memory_blocks",
                        user_id=_scope_user_id,
                        scope=_observe_scope,
                        dropped=_dropped_blocks,
                        table="agent_memory_blocks",
                    )
            except Exception as e:
                logger.debug("Interactive warmup observe-scope block count failed: %s", e)

    # Entity-aware context — if user mentions a name, pull relevant facts
    # Exclude the sender's name to avoid pulling facts about other people
    # who share the same name — the sender's identity comes from their
    # tenant's persona/user_profile blocks, not from entity search.
    if user_message and len(user_message) > 5:
        try:
            exclude = {exclude_name} if exclude_name else None
            context = _build_entity_context(
                user_message,
                tenant_id=tenant_id,
                exclude_names=exclude,
                scope=_enforce_scope,
                observe_scope_obj=_observe_scope,
                user_id=_scope_user_id,
            )
            if context:
                sections.append(context)
        except Exception as e:
            logger.debug("Interactive warmup entity context failed: %s", e)

    # Dynamic context hooks (date, travel, weather, etc.)
    try:
        situational = _run_context_hooks()
        if situational:
            sections.append(situational)
    except Exception as e:
        logger.debug("Interactive warmup context hooks failed: %s", e)

    # Main-only panoramic sections: open task queue + recent fleet surfaces.
    # These let the supervisor answer "what's going on?" from context alone.
    if agent_id == OPERATOR_INBOX_AGENT_ID:
        tasks_section = _open_tasks_section(
            tenant_id=tenant_id,
            scope=_enforce_scope,
            observe_scope_obj=_observe_scope,
            user_id=_scope_user_id,
        )
        if tasks_section:
            sections.append(tasks_section)
        fleet_section = _recent_fleet_surfaces(
            tenant_id=tenant_id,
            scope=_enforce_scope,
            observe_scope_obj=_observe_scope,
            user_id=_scope_user_id,
        )
        if fleet_section:
            sections.append(fleet_section)

    # Unified agent goal — every agent sees its own (no owner-only scoping).
    try:
        from robothor.engine.session_goal import build_agent_goal_context

        goal_section = build_agent_goal_context(tenant_id=tenant_id, agent_id=agent_id)
        if goal_section:
            sections.append(goal_section)
    except Exception as e:
        logger.debug("Interactive warmup agent_goal failed: %s", e)

    if not sections:
        return ""

    preamble = "\n\n".join(sections)
    if len(preamble) > MAX_WARMTH_CHARS:
        preamble = preamble[:MAX_WARMTH_CHARS] + "\n[warmup truncated]"

    _ack_surfaced_alerts(_surfaced_alert_ids, preamble, tenant_id)

    return preamble


MAX_ENTITY_CONTEXT_CHARS = 1000

# Words that are never entities (grammar) and capitalized sentence-starters that
# must not be mistaken for proper nouns.
_ENTITY_STOPWORDS = frozenset(
    {"the", "what", "how", "when", "where", "why", "can", "does", "did", "hey", "hi",
     "is", "are", "was", "were", "this", "that", "with", "for", "you", "your", "please",
     "and", "but", "about", "have", "has"}
)  # fmt: skip


def _warmup_recall_v2_enabled() -> bool:
    """Case-insensitive + recency-blended interactive entity recall (WS-5).

    Default OFF. The legacy path only extracts CAPITALIZED words (so a lowercase
    Telegram query surfaced nothing) and ordered hits by importance alone (so it
    returned the stalest high-importance alert and never the fresh event). When
    on, candidates are case-insensitive and hits are ranked by a recency/
    importance blend with a recency tilt, so the current state wins.
    """
    raw = os.environ.get("MEMORY_WARMUP_RECALL_V2", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _extract_entity_candidates(user_message: str, *, lowercase_ok: bool) -> list[str]:
    """Ordered candidate entity names from a message (pure, testable).

    Capitalized proper nouns first (highest precision). When ``lowercase_ok``,
    also append lowercase content words (len>=4) so a lowercase query still
    finds entities — the case-insensitive SQL match filters out non-entities.
    """
    import re

    capitalized: list[str] = []
    lowercased: list[str] = []
    seen: set[str] = set()
    for i, word in enumerate(user_message.split()):
        cleaned = re.sub(r"[^\w]", "", word)
        if len(cleaned) <= 2:
            continue
        low = cleaned.lower()
        if low in _ENTITY_STOPWORDS or low in seen:
            continue
        if cleaned[0].isupper() and (i > 0 or low not in _ENTITY_STOPWORDS):
            capitalized.append(cleaned)
            seen.add(low)
        elif lowercase_ok and cleaned.isalpha() and len(cleaned) >= 4:
            lowercased.append(cleaned)
            seen.add(low)
    return capitalized + lowercased


def _build_entity_context(
    user_message: str,
    tenant_id: str = DEFAULT_TENANT,
    exclude_names: set[str] | None = None,
    scope: DataScope | None = None,
    observe_scope_obj: DataScope | None = None,
    user_id: str | None = None,
) -> str:
    """Extract entities from the user message and pull relevant facts.

    Args:
        exclude_names: Names to skip during entity search (e.g. the current
            user's name, to avoid confusing them with other people).
        scope: "Own data + shared" DataScope (Task 5 / final-review Fix 1).
            ``None`` (the default — every pre-existing caller) issues the
            exact same SQL as before Task 5. A restricted scope adds
            ``AND (person_id = %s OR person_id IS NULL)`` to both candidate
            queries — see ``robothor.identity.scope`` and
            ``robothor.memory.facts.search_facts`` for the same pattern.
        observe_scope_obj: When set (observe mode + restricted identity),
            the query itself is NOT filtered (``scope`` should be ``None``
            in this case) but every fetched row is checked against this
            scope and a would-drop count is logged — never used to alter
            the returned text.
        user_id: Identity's stable id, for the observe-mode log line only.

    Budget: max 1000 chars. Stays synchronous (no async search_facts) so it can
    run inside the sync warmup path; recency awareness is done in SQL.
    """
    v2 = _warmup_recall_v2_enabled()
    candidates = _extract_entity_candidates(user_message, lowercase_ok=v2)

    if exclude_names:
        excluded = {n.lower() for n in exclude_names if n}
        candidates = [c for c in candidates if c.lower() not in excluded]

    if not candidates:
        return ""

    from psycopg2.extras import RealDictCursor

    from robothor.db import get_connection
    from robothor.identity.scope import log_would_drop, rows_dropped_by_scope

    scope_clause = ""
    scope_params: tuple[Any, ...] = ()
    if scope is not None and scope.restricted:
        scope_clause = "AND (person_id = %s OR person_id IS NULL)"
        scope_params = (scope.person_id,)

    lines = ["--- RELEVANT CONTEXT ---"]
    chars_used = 0
    # V2 looks at a few more candidates (lowercase queries are noisier) and
    # ranks by a recency/importance blend; legacy keeps the importance-only sort.
    max_candidates = 5 if v2 else 3
    _would_drop = 0

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for entity_name in candidates[:max_candidates]:
            if v2:
                cur.execute(
                    f"""
                    SELECT fact_text, category, importance_score, person_id
                    FROM memory_facts
                    WHERE is_active = TRUE AND tenant_id = %s
                      AND EXISTS (
                        SELECT 1 FROM unnest(entities) e WHERE lower(e) = lower(%s)
                      )
                      {scope_clause}
                    ORDER BY
                      0.55 * COALESCE(importance_score, 0.5)
                      + 0.45 * POWER(
                          0.5,
                          EXTRACT(EPOCH FROM (now() - created_at)) / 3600.0 / 168.0
                        ) DESC
                    LIMIT 3
                    """,
                    (tenant_id, entity_name, *scope_params),
                )
            else:
                cur.execute(
                    f"""
                    SELECT fact_text, category, importance_score, person_id
                    FROM memory_facts
                    WHERE is_active = TRUE AND %s = ANY(entities)
                      AND tenant_id = %s
                      {scope_clause}
                    ORDER BY importance_score DESC, created_at DESC
                    LIMIT 3
                    """,
                    (entity_name, tenant_id, *scope_params),
                )
            rows = cur.fetchall()
            if observe_scope_obj is not None:
                _would_drop += rows_dropped_by_scope(rows, observe_scope_obj)
            for f in rows:
                line = f"- {f['fact_text']}"
                if chars_used + len(line) > MAX_ENTITY_CONTEXT_CHARS:
                    break
                lines.append(line)
                chars_used += len(line)

    if observe_scope_obj is not None and _would_drop > 0:
        log_would_drop(
            tool_name="warmup:entity_context",
            user_id=user_id,
            scope=observe_scope_obj,
            dropped=_would_drop,
            table="memory_facts",
        )

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_peer_section(peer_agent_ids: list[str]) -> str:
    """Query peer agent schedules for recent status."""
    if not peer_agent_ids:
        return ""

    from robothor.engine.tracking import get_schedule

    lines = ["--- PEER AGENTS ---"]
    for peer_id in peer_agent_ids:
        try:
            schedule = get_schedule(peer_id)
            if not schedule:
                lines.append(f"{peer_id}: no data")
                continue

            status = schedule.get("last_status", "unknown")
            last_run = schedule.get("last_run_at", "")
            run_str = ""
            if last_run:
                if isinstance(last_run, datetime):
                    now = datetime.now(UTC)
                    delta = (
                        now - last_run.replace(tzinfo=UTC)
                        if last_run.tzinfo is None
                        else now - last_run
                    )
                    hours = delta.total_seconds() / 3600
                    run_str = f" ({hours:.1f}h ago)"
                else:
                    run_str = f" (at {last_run})"

            errors = schedule.get("consecutive_errors", 0)
            err_str = f" [{errors} errors]" if errors else ""

            lines.append(f"{peer_id}: {status}{run_str}{err_str}")
        except Exception as e:
            logger.debug("Failed to get peer schedule for %s: %s", peer_id, e)

    return "\n".join(lines) if len(lines) > 1 else ""


# ── Built-in context hooks (always active) ────────────────────────


_holidays_cache: dict[int, Any] = {}


def _get_us_holidays(year: int) -> Any:
    """Get cached US holidays object for a given year."""
    if year not in _holidays_cache:
        import holidays

        _holidays_cache[year] = holidays.US(years=year)
    return _holidays_cache[year]


def _date_context() -> str | None:
    """Current date, day of week, and upcoming US holidays."""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    day_name = today.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")
    result = f"Today: {day_name}, {date_str}"

    # Check for upcoming US holidays (next 7 days)
    try:
        us_holidays = _get_us_holidays(today.year)
        upcoming = []
        for delta in range(8):
            check = today + timedelta(days=delta)
            if check in us_holidays:
                name = us_holidays[check]
                if delta == 0:
                    upcoming.append(f"Today is {name}")
                elif delta == 1:
                    upcoming.append(f"Tomorrow is {name}")
                else:
                    upcoming.append(f"{name} in {delta} days ({check.strftime('%a %b %d')})")
        if upcoming:
            result += "\n" + "; ".join(upcoming)
    except ImportError:
        pass  # holidays package not installed — skip

    return result


def _travel_status() -> str | None:
    """Read travel_status memory block if non-empty."""
    try:
        from robothor.memory.blocks import read_block

        _tid = os.environ.get("ROBOTHOR_TENANT_ID", "") or DEFAULT_TENANT
        result = read_block("travel_status", tenant_id=_tid)
        content = (
            result.get("content", "") if isinstance(result, dict) else str(result) if result else ""
        )
        if content and content.strip():
            return f"Travel: {content.strip()[:200]}"
    except Exception:
        pass
    return None


def _weather_context() -> str | None:
    """Read weather status file if present."""
    try:
        _ws = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        weather_file = _ws / "brain" / "memory" / "weather-status.md"
        if weather_file.exists():
            content = weather_file.read_text().strip()
            if content:
                return f"Weather: {content[:200]}"
    except Exception:
        pass
    return None


def _run_agent_context_hooks(config: AgentConfig) -> str:
    """Run agent-aware context hooks, collecting results."""
    results: list[str] = []
    for hook in _AGENT_CONTEXT_HOOKS:
        try:
            start = time.monotonic()
            result = hook(config)
            elapsed = time.monotonic() - start
            if elapsed > 0.1:
                logger.debug("Agent hook %s took %.0fms", hook.__name__, elapsed * 1000)
            if result:
                results.append(result)
        except Exception as e:
            logger.debug("Agent context hook %s failed: %s", hook.__name__, e)
    return "\n".join(results) if results else ""


def _git_status_context(config: AgentConfig) -> str | None:
    """Git repo status for agents with git tools."""
    from robothor.engine.tools.constants import GIT_TOOLS

    agent_tools = set(config.tools_allowed) if config.tools_allowed else set()
    if not agent_tools & GIT_TOOLS:
        return None

    import subprocess

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    parts: list[str] = []
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            timeout=0.08,
            cwd=str(workspace),
        )
        if status.stdout.strip():
            parts.append(f"Branch & status:\n{status.stdout.strip()}")
    except Exception:
        pass
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True,
            text=True,
            timeout=0.08,
            cwd=str(workspace),
        )
        if log.stdout.strip():
            parts.append(f"Recent commits:\n{log.stdout.strip()}")
    except Exception:
        pass
    return "Git:\n" + "\n".join(parts) if parts else None


# Register built-in hooks on import
register_context_hook(_date_context)
register_context_hook(_travel_status)
register_context_hook(_weather_context)
register_agent_context_hook(_git_status_context)

from robothor.engine.thread_pool import _thread_pool_context  # noqa: E402

register_agent_context_hook(_thread_pool_context)
