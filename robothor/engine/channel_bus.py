"""Channel bus — dual-writes fleet Telegram deliveries into main's canonical
chat session so main has visibility of everything that reaches the user.

Wired up at daemon startup via `HookEvent.POST_DELIVERY`. The hook handler
is the single instrumentation point: `_deliver_telegram` in delivery.py
dispatches POST_DELIVERY after a successful send, and this module's
`on_post_delivery` decides whether to surface the message (non-main, opted in)
and writes both the chat_messages row and the channel_message_map entries.

Phase 1 scope (this module): write-only. No wake, no debounce.
Phases 2-3 (reply resolution + CHANNEL_EVENT wake) extend this module.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.chat import get_main_session_key
from robothor.engine.chat_store import save_channel_surface_async
from robothor.engine.hook_registry import HookContext, HookResult

logger = logging.getLogger(__name__)


@dataclass
class SurfaceRecord:
    """Metadata returned after a successful surface — used by reply resolution."""

    chat_message_id: int
    platform_message_ids: list[str]
    author_agent_id: str
    session_key: str


@dataclass
class _Bucket:
    """Pending surfaces in one (tenant, chat) debounce window."""

    run_ids: list[str]
    agents: list[str]
    timer_task: asyncio.Task[None] | None = None


class WakeDebouncer:
    """Collapse bursts of fleet surfaces into a single main wake.

    Each new surface into a (tenant, chat) bucket resets a timer. When the
    timer fires, the debouncer calls back into the scheduler to trigger a
    ``CHANNEL_EVENT`` run on main. Loop prevention:

      * Authorship filter upstream in ``on_post_delivery`` already skips
        main's own deliveries, so a main-authored reply never ends up here.
      * If main is already running when the timer fires, the scheduler's
        dedup check drops the wake and we set ``pending_wake`` so an
        ``AGENT_END`` hook can re-submit after main finishes.
      * A cooldown window (``main_wake_cooldown_seconds``) caps wake
        frequency — a too-recent prior wake defers the new batch.
    """

    def __init__(
        self,
        trigger_fn: Any,
        debounce_seconds: int = 15,
        cooldown_seconds: int = 300,
        rate_limit_per_hour: int = 20,
        enabled: bool = False,
    ) -> None:
        self._trigger_fn = trigger_fn
        self._debounce_seconds = debounce_seconds
        self._cooldown_seconds = cooldown_seconds
        self._rate_limit_per_hour = rate_limit_per_hour
        self._enabled = enabled

        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()

        # Rate limits — rolling-hour counter per agent
        self._agent_surfaces: dict[str, list[float]] = {}

        # Cooldown tracker — last wake fire time
        self._last_wake_at: float = 0.0

        # Main busy flag: set by AGENT_START on main, cleared by AGENT_END,
        # so we can re-submit pending work without chasing dedup directly.
        self._main_running: bool = False
        self._pending_key: tuple[str, str] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def check_rate_limit(self, agent_id: str) -> bool:
        """Per-agent rolling-hour rate limit. Returns True if the surface is
        allowed; False if it should be dropped."""
        import time as _time

        now = _time.time()
        bucket = self._agent_surfaces.setdefault(agent_id, [])
        cutoff = now - 3600
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= self._rate_limit_per_hour:
            return False
        bucket.append(now)
        return True

    async def submit(
        self,
        tenant_id: str,
        chat_id: str,
        agent_id: str,
        run_id: str,
    ) -> None:
        if not self._enabled:
            return
        key = (tenant_id, chat_id)
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(run_ids=[], agents=[])
                self._buckets[key] = bucket
            if run_id and run_id not in bucket.run_ids:
                bucket.run_ids.append(run_id)
            if agent_id and agent_id not in bucket.agents:
                bucket.agents.append(agent_id)
            if bucket.timer_task and not bucket.timer_task.done():
                bucket.timer_task.cancel()
            bucket.timer_task = asyncio.create_task(self._fire_after_delay(key))

    async def _fire_after_delay(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return

        async with self._lock:
            bucket = self._buckets.pop(key, None)
        if bucket is None:
            return

        import time as _time

        now = _time.time()
        if self._last_wake_at and (now - self._last_wake_at) < self._cooldown_seconds:
            logger.debug(
                "channel_bus wake cooldown active for %s (%.0fs remaining)",
                key,
                self._cooldown_seconds - (now - self._last_wake_at),
            )
            self._pending_key = key
            return

        if self._main_running:
            self._pending_key = key
            logger.debug("channel_bus wake deferred — main already running")
            return

        self._last_wake_at = now
        tenant_id, chat_id = key
        try:
            await self._trigger_fn(
                tenant_id=tenant_id,
                chat_id=chat_id,
                agents=list(bucket.agents),
                run_ids=list(bucket.run_ids),
            )
        except Exception as e:
            logger.warning("channel_bus wake trigger failed: %s", e)

    def mark_main_started(self) -> None:
        self._main_running = True

    async def mark_main_finished(self) -> None:
        self._main_running = False
        if self._pending_key:
            pending = self._pending_key
            self._pending_key = None
            # Fire immediately — the cooldown has already counted the missed wake.
            tenant_id, chat_id = pending
            try:
                await self._trigger_fn(
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    agents=[],
                    run_ids=[],
                )
            except Exception as e:
                logger.warning("channel_bus pending-wake fire failed: %s", e)


# Module-level singleton — wired up at daemon startup.
_debouncer: WakeDebouncer | None = None


def set_debouncer(debouncer: WakeDebouncer | None) -> None:
    global _debouncer
    _debouncer = debouncer


def get_debouncer() -> WakeDebouncer | None:
    return _debouncer


async def on_post_delivery(ctx: HookContext) -> HookResult:
    """POST_DELIVERY hook handler.

    ctx.metadata is expected to carry:
      chat_id: str                 — platform chat identifier
      platform_message_ids: list   — Telegram message_ids returned by send
      channel: str                 — e.g. "telegram"
      author_display_name: str     — config.name
      surface_to_channel: bool     — per-agent kill switch (default True)
      tenant_id: str               — run tenant
      session_key: str | None      — override target session (defaults to main)

    Behaviour:
      - Skip main's own deliveries (authorship filter — loop prevention).
      - Skip when surface_to_channel=False (operator kill switch).
      - Persist the output as an assistant turn carrying author_agent_id
        in main's canonical session.
      - Record one channel_message_map row per chunk so reply_to resolution
        can recover the original message later.
      - Publish an audit event to the Redis bus.
    """
    meta = ctx.metadata or {}
    agent_id = ctx.agent_id

    if not meta.get("surface_to_channel", True):
        return HookResult()

    if not ctx.output_text:
        return HookResult()

    # Authorship filter applies to the WAKE path only — main's own deliveries
    # must still be mapped (so replies to main's heartbeat resolve) and, when
    # they originate outside the interactive path (e.g. heartbeat), must also
    # land in the canonical channel session. What main must NOT do is trigger
    # a CHANNEL_EVENT on itself — that would loop. The submit to the debouncer
    # below is gated by this flag.
    is_main = agent_id == "main"
    # Interactive main turns are already persisted by telegram._run_interactive
    # via save_exchange → agent:main:primary. Only main-originating *heartbeat*
    # runs need the channel-session dual-write here. trigger_detail carries
    # "heartbeat:" as its prefix when scheduler._run_heartbeat routed the call.
    trigger_detail = meta.get("trigger_detail") or ""
    main_interactive = is_main and not trigger_detail.startswith("heartbeat:")
    skip_chat_write = main_interactive

    channel = meta.get("channel", "telegram")
    chat_id = meta.get("chat_id", "")
    platform_message_ids = [str(m) for m in meta.get("platform_message_ids", []) if m]
    author_display_name = meta.get("author_display_name", agent_id)
    tenant_id = meta.get("tenant_id") or DEFAULT_TENANT
    session_key = meta.get("session_key") or get_main_session_key()

    if not chat_id:
        logger.debug("channel_bus: no chat_id in metadata for %s, skipping", agent_id)
        return HookResult()

    debouncer = get_debouncer()
    if debouncer is not None and not debouncer.check_rate_limit(agent_id):
        logger.warning("channel_bus rate limit hit for %s — dropping surface", agent_id)
        return HookResult()

    chat_message_id: int | None = None
    if not skip_chat_write:
        try:
            chat_message_id = await save_channel_surface_async(
                session_key=session_key,
                content=ctx.output_text,
                author_agent_id=agent_id,
                author_display_name=author_display_name,
                surfaced_from_run_id=ctx.run_id or None,
                channel=channel,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning("channel_bus surface failed for %s: %s", agent_id, e)
            return HookResult()
    else:
        # Interactive main turn — find the assistant chat_messages row the
        # telegram handler just inserted so we can link the map to it.
        try:
            from robothor.db.connection import get_connection

            def _latest_main_assistant() -> int | None:
                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT cm.id FROM chat_messages cm
                        JOIN chat_sessions cs ON cs.id = cm.session_id
                        WHERE cs.tenant_id = %s AND cs.session_key = %s
                          AND cm.message->>'role' = 'assistant'
                        ORDER BY cm.id DESC LIMIT 1
                        """,
                        (tenant_id, session_key),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else None

            chat_message_id = await asyncio.get_running_loop().run_in_executor(
                None, _latest_main_assistant
            )
        except Exception as e:
            logger.debug("channel_bus main-interactive lookup failed: %s", e)

    if chat_message_id and platform_message_ids:
        # Fire-and-forget map write — don't block delivery return path
        asyncio.create_task(
            _record_outbound_async(
                tenant_id=tenant_id,
                channel=channel,
                chat_id=chat_id,
                platform_message_ids=platform_message_ids,
                session_key=session_key,
                chat_message_id=chat_message_id,
                author_agent_id=agent_id,
                author_run_id=ctx.run_id or None,
            )
        )

    try:
        from robothor.events.bus import publish

        publish(
            stream="channel",
            event_type="channel.surface",
            payload={
                "agent_id": agent_id,
                "run_id": ctx.run_id,
                "chat_id": chat_id,
                "platform_message_ids": platform_message_ids,
                "channel": channel,
                "tenant_id": tenant_id,
                "chat_message_id": chat_message_id,
            },
        )
    except Exception as e:
        logger.debug("channel_bus audit publish failed (non-fatal): %s", e)

    # Wake path: main never wakes itself. Debouncer submit is the only
    # authorship-filtered action in this handler.
    if debouncer is not None and not is_main:
        await debouncer.submit(
            tenant_id=tenant_id,
            chat_id=chat_id,
            agent_id=agent_id,
            run_id=ctx.run_id or "",
        )

    return HookResult()


def _link_message_to_crm(
    cur: Any,
    *,
    tenant_id: str,
    channel: str,
    chat_id: str,
    platform_message_id: str,
    session_key: str,
    chat_message_id: int | None,
    author_agent_id: str,
    direction: str,
) -> None:
    """Phase 2a write-through: link a recorded channel_message_map row to the
    Contact 360 fabric.

      * Resolve chat_id → person_id via contact_identifiers.
      * Stamp channel_message_map.person_id and chat_sessions.person_id.
      * Upsert message_thread (one per chat_id).
      * Insert message + message_participant.
      * Emit timeline_activity row.

    Best-effort: any failure is logged and swallowed so a CRM-side hiccup
    cannot break the primary chat flow. The caller's transaction wraps both
    the channel_message_map insert and these side effects.
    """
    try:
        # 1. Resolve person_id from contact_identifiers (live people only).
        cur.execute(
            """
            SELECT ci.person_id
              FROM contact_identifiers ci
              JOIN crm_people p ON p.id = ci.person_id
             WHERE ci.channel = %s AND ci.identifier = %s
               AND p.deleted_at IS NULL
             LIMIT 1
            """,
            (channel, chat_id),
        )
        row = cur.fetchone()
        person_id = (row[0] if not isinstance(row, dict) else row.get("person_id")) if row else None

        # 2-3. Stamp person_id on map + chat_sessions (only when known).
        if person_id is not None:
            cur.execute(
                """
                UPDATE channel_message_map
                   SET person_id = %s
                 WHERE tenant_id = %s AND channel = %s
                   AND chat_id = %s AND platform_message_id = %s
                   AND person_id IS NULL
                """,
                (person_id, tenant_id, channel, chat_id, str(platform_message_id)),
            )
            cur.execute(
                """
                UPDATE chat_sessions
                   SET person_id = %s
                 WHERE tenant_id = %s AND session_key = %s
                   AND person_id IS NULL
                """,
                (person_id, tenant_id, session_key),
            )

        # 4. UPSERT message_thread.
        cur.execute(
            """
            INSERT INTO message_thread
                (tenant_id, channel, external_thread_id, last_message_at, message_count)
            VALUES (%s, %s, %s, NOW(), 1)
            ON CONFLICT (tenant_id, channel, external_thread_id)
            DO UPDATE SET last_message_at = EXCLUDED.last_message_at,
                          message_count   = message_thread.message_count + 1,
                          updated_at      = NOW()
            RETURNING id
            """,
            (tenant_id, channel, chat_id),
        )
        tr = cur.fetchone()
        thread_id = tr[0] if not isinstance(tr, dict) else tr["id"]

        # Pull message body from chat_messages.message JSONB for snippet.
        body_text: str | None = None
        if chat_message_id is not None:
            cur.execute(
                "SELECT message FROM chat_messages WHERE id = %s",
                (chat_message_id,),
            )
            br = cur.fetchone()
            if br is not None:
                payload = br[0] if not isinstance(br, dict) else br["message"]
                if isinstance(payload, dict):
                    body_text = payload.get("content")

        # 5. INSERT message (idempotent on (tenant, channel, external_message_id)).
        cur.execute(
            """
            INSERT INTO message
                (tenant_id, thread_id, channel, direction,
                 external_message_id, body_text, snippet, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (tenant_id, channel, external_message_id)
            DO UPDATE SET thread_id = EXCLUDED.thread_id
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                tenant_id,
                thread_id,
                channel,
                direction,
                str(platform_message_id),
                body_text,
                (body_text or "")[:200] if body_text else None,
            ),
        )
        mr = cur.fetchone()
        if isinstance(mr, dict):
            message_id = mr["id"]
            inserted = mr["inserted"]
        else:
            message_id, inserted = mr[0], mr[1]

        if not inserted:
            # Already linked previously — no participant or timeline duplication.
            return

        # 6. INSERT message_participant.
        role = "from" if direction == "inbound" else "to"
        cur.execute(
            """
            INSERT INTO message_participant
                (tenant_id, message_id, role, person_id, handle)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (tenant_id, message_id, role, person_id, chat_id),
        )

        # 7. INSERT timeline_activity (idempotent on source_table+source_id).
        if person_id is not None:
            activity_type = (
                "telegram_message"
                if channel == "telegram"
                else "sms"
                if channel == "sms"
                else "webchat_message"
                if channel == "webchat"
                else "conversation_message"
            )
            cur.execute(
                """
                INSERT INTO timeline_activity
                    (tenant_id, person_id, occurred_at, activity_type,
                     source_table, source_id, channel, direction, agent_id, snippet)
                VALUES (%s, %s, NOW(), %s, 'message', %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                """,
                (
                    tenant_id,
                    person_id,
                    activity_type,
                    str(message_id),
                    channel,
                    direction,
                    None if author_agent_id == "user" else author_agent_id,
                    (body_text or "")[:200] if body_text else None,
                ),
            )
    except Exception as e:  # noqa: BLE001 — best-effort write-through
        logger.warning(
            "channel_bus CRM link failed (channel=%s chat=%s pmid=%s dir=%s): %s",
            channel,
            chat_id,
            platform_message_id,
            direction,
            e,
        )


def record_outbound(
    tenant_id: str,
    channel: str,
    chat_id: str,
    platform_message_ids: list[str],
    session_key: str,
    chat_message_id: int,
    author_agent_id: str,
    author_run_id: str | None = None,
) -> None:
    """Write one channel_message_map row per platform_message_id. Sync; wrap
    with run_in_executor from async contexts."""
    from robothor.db.connection import get_connection

    if not platform_message_ids:
        return

    with get_connection() as conn:
        cur = conn.cursor()
        for pmid in platform_message_ids:
            cur.execute(
                """
                INSERT INTO channel_message_map
                    (tenant_id, channel, chat_id, platform_message_id,
                     session_key, chat_message_id, author_agent_id,
                     author_run_id, direction)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'outbound')
                ON CONFLICT (tenant_id, channel, chat_id, platform_message_id)
                DO NOTHING
                """,
                (
                    tenant_id,
                    channel,
                    chat_id,
                    str(pmid),
                    session_key,
                    chat_message_id,
                    author_agent_id,
                    author_run_id,
                ),
            )
            _link_message_to_crm(
                cur,
                tenant_id=tenant_id,
                channel=channel,
                chat_id=chat_id,
                platform_message_id=str(pmid),
                session_key=session_key,
                chat_message_id=chat_message_id,
                author_agent_id=author_agent_id,
                direction="outbound",
            )
        conn.commit()


async def _record_outbound_async(**kwargs: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: record_outbound(**kwargs))
    except Exception as e:
        logger.warning(
            "channel_bus map write failed for %s: %s",
            kwargs.get("author_agent_id"),
            e,
        )


def record_inbound(
    tenant_id: str,
    channel: str,
    chat_id: str,
    platform_message_id: str,
    session_key: str,
    chat_message_id: int | None = None,
    author_agent_id: str = "user",
) -> None:
    """Record an inbound (user-authored) message in the channel map so
    future replies or history queries can recover its platform id. Sync —
    wrap with run_in_executor from async contexts."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO channel_message_map
                (tenant_id, channel, chat_id, platform_message_id,
                 session_key, chat_message_id, author_agent_id,
                 author_run_id, direction)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, 'inbound')
            ON CONFLICT (tenant_id, channel, chat_id, platform_message_id)
            DO NOTHING
            """,
            (
                tenant_id,
                channel,
                chat_id,
                str(platform_message_id),
                session_key,
                chat_message_id,
                author_agent_id,
            ),
        )
        _link_message_to_crm(
            cur,
            tenant_id=tenant_id,
            channel=channel,
            chat_id=chat_id,
            platform_message_id=str(platform_message_id),
            session_key=session_key,
            chat_message_id=chat_message_id,
            author_agent_id=author_agent_id,
            direction="inbound",
        )
        conn.commit()


async def record_inbound_async(**kwargs: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: record_inbound(**kwargs))
    except Exception as e:
        logger.debug("channel_bus inbound map write failed (non-fatal): %s", e)


async def resolve_reply_context_async(
    chat_id: str,
    platform_message_id: str,
    channel: str = "telegram",
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Async wrapper around resolve_reply_context so the hot Telegram path
    doesn't block on the DB lookup."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: resolve_reply_context(
                chat_id=chat_id,
                platform_message_id=platform_message_id,
                channel=channel,
                tenant_id=tenant_id,
            ),
        )
    except Exception as e:
        logger.debug("resolve_reply_context_async failed (non-fatal): %s", e)
        return None


def format_reply_prefix(reply_ctx: dict[str, Any]) -> str:
    """Build the quote prefix we prepend to the user's message when they
    reply to a surfaced fleet output.

    Explicit intent signal: the quoted content is *reference*, not a work
    item. Observed failure mode without this: main treated the quoted
    heartbeat as instructions and proposed unrelated actions on it. The
    two-line shape (intent header + attribution + quote) gives the LLM a
    clearer frame.
    """
    author = reply_ctx.get("author_display_name") or reply_ctx.get("author_agent_id") or "agent"
    snippet = (reply_ctx.get("content_snippet") or "").strip().replace("\n", " ")
    if len(snippet) > 180:
        snippet = snippet[:177] + "..."
    return (
        "[User is referencing this earlier message — "
        "answer questions about it, do not act unless explicitly asked]\n"
        f'@{author} said: "{snippet}"'
    )


def resolve_reply_context(
    chat_id: str,
    platform_message_id: str,
    channel: str = "telegram",
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Look up a platform message_id in channel_message_map and return the
    author + content snippet for prefixing the user's reply prompt. Returns
    None if the message wasn't one we recorded (e.g. sent out-of-band, or
    pre-channel-bus). Used by Phase 2 reply resolution in telegram.handle_text.
    """
    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.chat_message_id,
                       m.author_agent_id,
                       m.author_run_id,
                       m.session_key,
                       c.message
                FROM channel_message_map m
                JOIN chat_messages c ON c.id = m.chat_message_id
                WHERE m.tenant_id = %s
                  AND m.channel = %s
                  AND m.chat_id = %s
                  AND m.platform_message_id = %s
                ORDER BY m.created_at DESC
                LIMIT 1
                """,
                (tenant_id, channel, chat_id, str(platform_message_id)),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.debug("resolve_reply_context lookup failed: %s", e)
        return None

    if not row:
        return None

    chat_message_id, author_agent_id, author_run_id, session_key, message = row
    content = ""
    display_name = ""
    if isinstance(message, dict):
        content = message.get("content") or ""
        display_name = message.get("author_display_name") or ""

    return {
        "chat_message_id": chat_message_id,
        "author_agent_id": author_agent_id,
        "author_display_name": display_name or author_agent_id,
        "author_run_id": author_run_id,
        "session_key": session_key,
        "platform_message_id": str(platform_message_id),
        "content": content,
        "content_snippet": content[:200],
    }
