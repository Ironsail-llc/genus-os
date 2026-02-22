"""
Base consumer class for event bus stream processing.

Provides common lifecycle management, signal handling, and error recovery.
Subclasses override `handle()` to process events.

Usage:
    class MyConsumer(BaseConsumer):
        stream = "email"
        group = "email-pipeline"
        consumer_name = "worker-1"

        def handle(self, event: dict) -> None:
            print(event["type"], event["payload"])

    if __name__ == "__main__":
        MyConsumer().run()
"""

from __future__ import annotations

import logging
import os
import signal
from abc import ABC, abstractmethod

from robothor.events.bus import subscribe

logger = logging.getLogger(__name__)


class BaseConsumer(ABC):
    """Base class for event bus consumers.

    Attributes:
        stream: Redis stream name (email, calendar, crm, vision, health, agent, system)
        group: Consumer group name (for exactly-once delivery within the group)
        consumer_name: Unique consumer name within the group
        batch_size: Number of messages to read per iteration
        block_ms: How long to block waiting for new messages (ms)
    """

    stream: str
    group: str
    consumer_name: str = "worker-0"
    batch_size: int = 10
    block_ms: int = 5000

    def __init__(self) -> None:
        self._running = True
        # Allow override from env vars
        self.consumer_name = os.environ.get(
            "CONSUMER_NAME", self.consumer_name
        )

    @abstractmethod
    def handle(self, event: dict) -> None:
        """Process a single event.

        Args:
            event: Parsed event dict with keys:
                - id: Stream message ID
                - timestamp: ISO 8601
                - type: Event type string
                - source: Producing script/service
                - actor: Agent or system identity
                - payload: Parsed dict
                - correlation_id: Optional trace ID

        On success, the event is auto-acknowledged.
        On exception, the event is NOT acknowledged and will be retried.
        """

    def on_start(self) -> None:  # noqa: B027
        """Hook called before the consumer loop starts. Override for setup."""

    def on_stop(self) -> None:  # noqa: B027
        """Hook called after the consumer loop ends. Override for cleanup."""

    def _signal_handler(self, signum: int, _frame: object) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        name = signal.Signals(signum).name
        logger.info("%s consumer received %s, shutting down", self.stream, name)
        self._running = False

    def run(self, max_iterations: int | None = None) -> None:
        """Start the blocking subscribe loop.

        Args:
            max_iterations: Stop after N iterations. None = infinite (production).
        """
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(
            "Starting %s consumer: stream=%s group=%s consumer=%s",
            self.__class__.__name__,
            self.stream,
            self.group,
            self.consumer_name,
        )

        self.on_start()

        try:
            subscribe(
                self.stream,
                self.group,
                self.consumer_name,
                handler=self.handle,
                batch_size=self.batch_size,
                block_ms=self.block_ms,
                max_iterations=max_iterations,
            )
        except KeyboardInterrupt:
            logger.info("%s consumer interrupted", self.stream)
        finally:
            self.on_stop()
            logger.info("%s consumer stopped", self.stream)
