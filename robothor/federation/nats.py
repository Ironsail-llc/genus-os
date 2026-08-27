"""NATS infrastructure — server lifecycle, leaf node management, subject routing.

NATS handles all inter-instance communication with JetStream for delivery
guarantees. Each connection gets its own account for isolation.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.federation.models import SyncChannel

logger = logging.getLogger(__name__)

# Subject namespace pattern
SUBJECT_PREFIX = "robothor"


def subject_for(connection_id: str, channel: SyncChannel) -> str:
    """Build the NATS subject for a connection + channel."""
    return f"{SUBJECT_PREFIX}.{connection_id}.sync.{channel.value}"


def command_subject(connection_id: str) -> str:
    """Build the command subject for a connection."""
    return f"{SUBJECT_PREFIX}.{connection_id}.command"


def status_subject(connection_id: str) -> str:
    """Build the status subject for a connection."""
    return f"{SUBJECT_PREFIX}.{connection_id}.status"


class NATSManager:
    """Manages NATS connections for federation.

    Handles publishing sync events and subscribing to incoming events
    from connected peers.
    """

    #: Option keys that carry a secret and must never reach a log line.
    _SECRET_OPTIONS = frozenset({"password", "token", "creds", "nkey_seed"})

    def __init__(self, nats_url: str = "nats://127.0.0.1:4222", **options: Any) -> None:
        self._nats_url = nats_url
        # Credentials to present on connect. `nats.connect(url)` presented
        # nothing, which was invisible while the server had no `authorization`
        # block: every client, including any leafnode, landed in the same
        # global account with reach into JetStream.
        self._options = {k: v for k, v in options.items() if v}
        self._nc: Any = None  # nats.aio.client.Client
        self._js: Any = None  # JetStream context
        self._subscriptions: dict[str, Any] = {}

    def __repr__(self) -> str:
        shown = {k: ("***" if k in self._SECRET_OPTIONS else v) for k, v in self._options.items()}
        return f"NATSManager(url={self._nats_url!r}, options={shown})"

    @property
    def url(self) -> str:
        return self._nats_url

    async def connect(self) -> bool:
        """Connect to NATS server, presenting this endpoint's credential."""
        try:
            import nats

            # Bounded by default. A bad credential against a server that DOES
            # have an authorization block retries 60 times and blocks daemon
            # startup for a minute; the operator wants to know now, and the
            # transport reports the failure rather than swallowing it.
            options = {"connect_timeout": 5, "max_reconnect_attempts": 10, **self._options}
            self._nc = await nats.connect(self._nats_url, **options)
            self._js = self._nc.jetstream()
            logger.info(
                "Connected to NATS at %s (auth: %s)",
                self._nats_url,
                ", ".join(sorted(self._options)) or "none",
            )
            return True
        except ImportError:
            logger.warning("nats-py not installed — federation transport unavailable")
            return False
        except Exception as e:
            # The exception repr can carry the credential on some auth errors.
            logger.warning("Failed to connect to NATS at %s: %s", self._nats_url, type(e).__name__)
            return False

    async def disconnect(self) -> None:
        """Disconnect from NATS."""
        if self._nc:
            try:
                await self._nc.drain()
            except Exception as e:
                logger.warning("NATS drain failed: %s", e)
            self._nc = None
            self._js = None

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def ensure_stream(self, connection_id: str) -> bool:
        """Ensure JetStream streams exist for a connection."""
        if not self._js:
            return False

        try:
            import nats.js.api as js_api

            stream_name = f"fed_{connection_id.replace('-', '_')[:32]}"

            subjects = [
                subject_for(connection_id, SyncChannel.CRITICAL),
                subject_for(connection_id, SyncChannel.BULK),
                subject_for(connection_id, SyncChannel.MEDIA),
                command_subject(connection_id),
                status_subject(connection_id),
            ]

            await self._js.add_stream(
                js_api.StreamConfig(
                    name=stream_name,
                    subjects=subjects,
                    retention=js_api.RetentionPolicy.LIMITS,
                    max_msgs=100_000,
                    max_bytes=100 * 1024 * 1024,  # 100 MB
                    storage=js_api.StorageType.FILE,
                    discard=js_api.DiscardPolicy.OLD,
                ),
            )
            logger.info("JetStream stream %s ensured for connection %s", stream_name, connection_id)
            return True
        except Exception as e:
            logger.warning("Failed to ensure stream for %s: %s", connection_id, e)
            return False

    async def publish(
        self,
        connection_id: str,
        channel: SyncChannel,
        data: bytes,
    ) -> bool:
        """Publish a sync event to a connection's channel."""
        if not self._js:
            return False

        subject = subject_for(connection_id, channel)
        try:
            await self._js.publish(subject, data)
            return True
        except Exception as e:
            logger.warning("NATS publish failed on %s: %s", subject, e)
            return False

    async def publish_command(self, connection_id: str, data: bytes) -> bool:
        """Publish a command to a connected peer."""
        if not self._nc:
            return False

        subject = command_subject(connection_id)
        try:
            await self._nc.publish(subject, data)
            return True
        except Exception as e:
            logger.warning("NATS command publish failed on %s: %s", subject, e)
            return False

    async def request(self, connection_id: str, data: bytes, timeout: float = 5.0) -> bytes | None:
        """Send a request to a peer and await its reply (NATS request-reply).

        Returns the reply bytes, or None on timeout / not-connected. Used by the
        federation_query/trigger tools for synchronous remote calls.
        """
        if not self._nc:
            return None
        subject = command_subject(connection_id)
        try:
            reply = await self._nc.request(subject, data, timeout=timeout)
            return bytes(reply.data)
        except Exception as e:
            logger.warning("NATS request failed on %s: %s", subject, e)
            return None

    async def serve_requests(self, connection_id: str, handler: Any) -> bool:
        """Respond to peer requests on this connection's command subject.

        ``handler`` is an async callable ``(bytes) -> bytes`` that executes a
        read-only/triggered action and returns the reply payload.
        """
        if not self._nc:
            return False
        subject = command_subject(connection_id)

        async def _on_request(msg: Any) -> None:
            try:
                reply = await handler(msg.data)
            except Exception as e:
                logger.warning("Federation request handler failed: %s", e)
                reply = b'{"error": "handler failed"}'
            if msg.reply:
                await self._nc.publish(msg.reply, reply)

        try:
            await self._nc.subscribe(subject, cb=_on_request)
            return True
        except Exception as e:
            logger.warning("NATS serve_requests failed on %s: %s", subject, e)
            return False

    async def subscribe(
        self,
        connection_id: str,
        channel: SyncChannel,
        callback: Any,
    ) -> bool:
        """Subscribe to a connection's sync channel."""
        if not self._js:
            return False

        subject = subject_for(connection_id, channel)
        try:
            sub = await self._js.subscribe(
                subject,
                durable=f"fed_{connection_id[:16]}_{channel.value}",
                cb=callback,
            )
            self._subscriptions[f"{connection_id}:{channel.value}"] = sub
            return True
        except Exception as e:
            logger.warning("NATS subscribe failed on %s: %s", subject, e)
            return False

    async def subscribe_commands(self, connection_id: str, callback: Any) -> bool:
        """Subscribe to incoming commands from a peer."""
        if not self._nc:
            return False

        subject = command_subject(connection_id)
        try:
            sub = await self._nc.subscribe(subject, cb=callback)
            self._subscriptions[f"{connection_id}:command"] = sub
            return True
        except Exception as e:
            logger.warning("NATS command subscribe failed on %s: %s", subject, e)
            return False

    async def unsubscribe_all(self, connection_id: str) -> None:
        """Unsubscribe from all subjects for a connection."""
        import contextlib

        keys_to_remove = [k for k in self._subscriptions if k.startswith(f"{connection_id}:")]
        for key in keys_to_remove:
            sub = self._subscriptions.pop(key, None)
            if sub:
                with contextlib.suppress(Exception):
                    await sub.unsubscribe()

    async def publish_status(self, connection_id: str, data: bytes) -> bool:
        """Publish a status/health update (piggybacked on status subject)."""
        if not self._nc:
            return False

        subject = status_subject(connection_id)
        try:
            await self._nc.publish(subject, data)
            return True
        except Exception as e:
            logger.warning("NATS status publish failed: %s", e)
            return False


# ── Module singleton ──────────────────────────────────────────────────
# Set by the daemon at startup when federation (NATS) is enabled, so the
# federation_query / federation_trigger tools can reach the connected manager.
_nats_manager: NATSManager | None = None


def get_nats_manager() -> NATSManager | None:
    """Return the connected NATSManager singleton, or None if federation is off."""
    return _nats_manager


def set_nats_manager(manager: NATSManager | None) -> None:
    """Register (or clear) the NATSManager singleton."""
    global _nats_manager
    _nats_manager = manager
