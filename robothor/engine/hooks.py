"""
Event Hooks — Redis Stream consumers that trigger agent runs.

Subscribes to Redis streams (email, calendar, etc.) and invokes
agents when matching events arrive.

Publishers (event_bus.py) write to "robothor:events:<stream>" keys.
This consumer subscribes to those same prefixed keys.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from robothor.engine.dedup import try_acquire, release
from robothor.engine.delivery import deliver
from robothor.engine.models import TriggerType

MAX_RETRIES = 3

# Must match event_bus.STREAM_PREFIX — publishers write to these keys
STREAM_PREFIX = "robothor:events:"

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

# Legacy fallback — used only when no manifests define hooks.
# Once all hooks are in manifests, this dict can be removed.
_LEGACY_EVENT_TRIGGERS: dict[str, list[dict]] = {
    "email": [
        {
            "event_type": "email.new",
            "agent_id": "email-classifier",
            "message": "New email received. Process the triage inbox and classify emails.",
        },
    ],
    "calendar": [
        {
            "event_type": "calendar.new",
            "agent_id": "calendar-monitor",
            "message": "New calendar event. Check for conflicts, cancellations, and changes.",
        },
        {
            "event_type": "calendar.rescheduled",
            "agent_id": "calendar-monitor",
            "message": "Calendar event rescheduled. Check for conflicts and update accordingly.",
        },
        {
            "event_type": "calendar.modified",
            "agent_id": "calendar-monitor",
            "message": "Calendar event modified. Check for conflicts, cancellations, and changes.",
        },
    ],
    "vision": [
        {
            "event_type": "vision.person_unknown",
            "agent_id": "vision-monitor",
            "message": "Unknown person detected. Check vision events and escalate if needed.",
        },
    ],
}


def build_event_triggers(manifest_dir) -> dict[str, list[dict]]:
    """Build event trigger map from agent manifests.

    Scans all manifests for `hooks` entries and aggregates them into a
    stream → triggers dict. Falls back to _LEGACY_EVENT_TRIGGERS if no
    manifests define hooks.
    """
    from pathlib import Path

    from robothor.engine.config import load_all_manifests, manifest_to_agent_config

    triggers: dict[str, list[dict]] = {}
    manifest_dir = Path(manifest_dir)

    if not manifest_dir.is_dir():
        logger.warning("Manifest dir not found for hooks: %s", manifest_dir)
        return dict(_LEGACY_EVENT_TRIGGERS)

    manifests = load_all_manifests(manifest_dir)
    for raw in manifests:
        config = manifest_to_agent_config(raw)
        for hook in config.hooks:
            entry = {
                "event_type": hook.event_type,
                "agent_id": config.id,
                "message": hook.message,
            }
            triggers.setdefault(hook.stream, []).append(entry)

    if triggers:
        total = sum(len(v) for v in triggers.values())
        logger.info(
            "Built %d event triggers from manifests (%d streams)",
            total,
            len(triggers),
        )
        return triggers

    # Fallback: no manifests define hooks yet
    logger.info("No manifest hooks found, using legacy event triggers")
    return dict(_LEGACY_EVENT_TRIGGERS)


# Module-level reference for backward compatibility with tests
# Populated at import time from legacy; overridden at runtime by start()
EVENT_TRIGGERS = dict(_LEGACY_EVENT_TRIGGERS)


class EventHooks:
    """Redis Stream consumer that triggers agent runs on events."""

    def __init__(
        self,
        config: EngineConfig,
        runner: AgentRunner,
        workflow_engine=None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.workflow_engine = workflow_engine
        self._stop_event = asyncio.Event()
        self._prefixed_to_bare: dict[str, str] = {}
        self._event_triggers: dict[str, list[dict]] = {}

    async def start(self) -> None:
        """Start consuming Redis streams."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning("redis.asyncio not available, event hooks disabled")
            await self._idle()
            return

        from robothor.config import get_config

        cfg = get_config()

        try:
            r = aioredis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=cfg.redis.password or None,
            )
            await r.ping()
            logger.info("Event hooks connected to Redis")
        except Exception as e:
            logger.warning("Redis not available, event hooks disabled: %s", e)
            await self._idle()
            return

        consumer_group = "engine"
        consumer_name = f"engine-{self.config.tenant_id}"

        # Build triggers from manifests (with legacy fallback)
        self._event_triggers = build_event_triggers(self.config.manifest_dir)
        bare_streams = list(self._event_triggers.keys())

        # Build prefixed stream keys and a reverse lookup
        prefixed_streams = [f"{STREAM_PREFIX}{s}" for s in bare_streams]
        self._prefixed_to_bare = {
            f"{STREAM_PREFIX}{s}": s for s in bare_streams
        }

        # Create consumer groups on the prefixed keys
        for prefixed in prefixed_streams:
            try:
                await r.xgroup_create(
                    prefixed, consumer_group, id="$", mkstream=True
                )
            except Exception:
                pass  # Group already exists

        logger.info("Listening on streams: %s", ", ".join(prefixed_streams))

        # Read loop — subscribe to prefixed keys
        stream_keys = {s: ">" for s in prefixed_streams}
        while not self._stop_event.is_set():
            try:
                results = await r.xreadgroup(
                    consumer_group,
                    consumer_name,
                    stream_keys,
                    count=5,
                    block=5000,
                )
                if not results:
                    continue

                for stream_name, messages in results:
                    stream_str = (
                        stream_name.decode()
                        if isinstance(stream_name, bytes)
                        else stream_name
                    )
                    for msg_id, data in messages:
                        await self._handle_event(
                            stream_str, msg_id, data, r, consumer_group
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Event hook error: %s", e, exc_info=True)
                await asyncio.sleep(5)

        await r.aclose()

    async def _handle_event(
        self,
        stream: str,
        msg_id,
        data: dict,
        redis_client,
        group: str,
    ) -> None:
        """Process a single event from a stream."""
        # Decode bytes
        decoded = {}
        for k, v in data.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            decoded[key] = val

        # event_bus._make_envelope() stores the event type under "type"
        event_type = decoded.get("type", "")

        # Strip prefix to get bare stream name for trigger lookup
        bare_stream = self._prefixed_to_bare.get(stream, stream)
        triggers = self._event_triggers.get(bare_stream, [])

        failed = False
        for trigger in triggers:
            if event_type and trigger.get("event_type") != event_type:
                continue

            agent_id = trigger["agent_id"]

            # Dedup: skip if agent is already running (shared with scheduler)
            if not try_acquire(agent_id):
                logger.debug("Skipping %s — already running", agent_id)
                continue

            try:
                from robothor.engine.config import load_agent_config

                agent_config = load_agent_config(
                    agent_id, self.config.manifest_dir
                )

                # Build warm message with preamble
                message = trigger["message"]
                if agent_config:
                    try:
                        from robothor.engine.warmup import build_warmth_preamble

                        preamble = build_warmth_preamble(
                            agent_config,
                            self.config.workspace,
                            self.config.tenant_id,
                        )
                        if preamble:
                            message = f"{preamble}\n\n{message}"
                    except Exception as e:
                        logger.debug(
                            "Warmup preamble failed for hook %s: %s",
                            agent_id,
                            e,
                        )

                run = await self.runner.execute(
                    agent_id=agent_id,
                    message=message,
                    trigger_type=TriggerType.HOOK,
                    trigger_detail=f"{bare_stream}:{event_type}",
                    agent_config=agent_config,
                )

                if agent_config:
                    await deliver(agent_config, run)

                if run.status.value in ("failed", "timeout"):
                    failed = True
                elif (
                    run.status.value == "completed"
                    and agent_config
                    and agent_config.downstream_agents
                ):
                    # Trigger downstream agents (fire-and-forget)
                    for downstream_id in agent_config.downstream_agents:
                        logger.info(
                            "Hook triggering downstream agent: %s",
                            downstream_id,
                        )
                        asyncio.create_task(
                            self._trigger_downstream(
                                downstream_id, bare_stream, event_type
                            )
                        )

                logger.info(
                    "Hook complete: %s via %s:%s status=%s",
                    agent_id,
                    bare_stream,
                    event_type,
                    run.status.value,
                )
            except Exception as e:
                logger.error(
                    "Hook execution failed for %s: %s", agent_id, e
                )
                failed = True
            finally:
                release(agent_id)

        # Dispatch matching workflows (fire-and-forget, parallel to agents)
        if self.workflow_engine:
            matching_workflows = self.workflow_engine.get_workflows_for_event(
                bare_stream, event_type
            )
            for wf in matching_workflows:
                logger.info(
                    "Hook triggering workflow: %s via %s:%s",
                    wf.id, bare_stream, event_type,
                )
                asyncio.create_task(
                    self._run_workflow(wf.id, bare_stream, event_type)
                )

        # Dead letter queue on failure
        if failed:
            retry_count = int(decoded.get("_retry_count", "0"))
            if retry_count < MAX_RETRIES:
                try:
                    retry_data = dict(decoded)
                    retry_data["_retry_count"] = str(retry_count + 1)
                    await redis_client.xadd(stream, retry_data)
                    logger.info(
                        "Retrying %s:%s (attempt %d/%d)",
                        stream,
                        event_type,
                        retry_count + 1,
                        MAX_RETRIES,
                    )
                except Exception as e:
                    logger.warning("Failed to re-queue for retry: %s", e)
            else:
                try:
                    dlq_stream = f"{stream}:dlq"
                    await redis_client.xadd(
                        dlq_stream, decoded, maxlen=1000
                    )
                    logger.warning(
                        "Moved to DLQ %s after %d retries: %s",
                        dlq_stream,
                        MAX_RETRIES,
                        event_type,
                    )
                except Exception as e:
                    logger.warning("Failed to add to DLQ: %s", e)

        # Acknowledge the original message
        try:
            await redis_client.xack(stream, group, msg_id)
        except Exception as e:
            logger.warning("Failed to ack message %s: %s", msg_id, e)

    async def _trigger_downstream(
        self,
        agent_id: str,
        source_stream: str,
        source_event: str,
    ) -> None:
        """Trigger a downstream agent after a successful hook run.

        Mirrors scheduler._run_agent pattern: load config, build warmth,
        execute, deliver. Uses try_acquire/release for dedup.
        """
        if not try_acquire(agent_id):
            logger.debug("Downstream %s skipped — already running", agent_id)
            return

        try:
            from robothor.engine.config import load_agent_config

            agent_config = load_agent_config(
                agent_id, self.config.manifest_dir
            )
            if not agent_config:
                logger.warning(
                    "Downstream agent config not found: %s", agent_id
                )
                return

            message = (
                f"Triggered as downstream agent after "
                f"{source_stream}:{source_event}. Execute your tasks."
            )
            try:
                from robothor.engine.warmup import build_warmth_preamble

                preamble = build_warmth_preamble(
                    agent_config,
                    self.config.workspace,
                    self.config.tenant_id,
                )
                if preamble:
                    message = f"{preamble}\n\n{message}"
            except Exception as e:
                logger.debug(
                    "Warmup failed for downstream %s: %s", agent_id, e
                )

            run = await self.runner.execute(
                agent_id=agent_id,
                message=message,
                trigger_type=TriggerType.HOOK,
                trigger_detail=f"downstream:{source_stream}:{source_event}",
                agent_config=agent_config,
            )

            await deliver(agent_config, run)

            logger.info(
                "Downstream complete: %s status=%s",
                agent_id,
                run.status.value,
            )
        except Exception as e:
            logger.error(
                "Downstream execution failed for %s: %s", agent_id, e
            )
        finally:
            release(agent_id)

    async def _run_workflow(
        self, workflow_id: str, stream: str, event_type: str
    ) -> None:
        """Execute a workflow triggered by an event (fire-and-forget)."""
        try:
            await self.workflow_engine.execute(
                workflow_id=workflow_id,
                trigger_type="hook",
                trigger_detail=f"{stream}:{event_type}",
            )
        except Exception as e:
            logger.error(
                "Workflow %s failed (hook %s:%s): %s",
                workflow_id, stream, event_type, e,
            )

    async def _idle(self) -> None:
        """Idle loop when Redis is not available."""
        while not self._stop_event.is_set():
            await asyncio.sleep(60)

    async def stop(self) -> None:
        """Signal the event loop to stop."""
        self._stop_event.set()
