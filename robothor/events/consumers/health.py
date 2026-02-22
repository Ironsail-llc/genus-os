"""
Health consumer — monitors service health events and escalates degraded services.

On service.health events with degraded status, logs the issue and optionally
sends alerts via configured webhook.

Run as: python -m robothor.events.consumers.health
"""

from __future__ import annotations

import logging
import os

import httpx

from robothor.audit.logger import log_event
from robothor.events.consumers.base import BaseConsumer

logger = logging.getLogger(__name__)


class HealthConsumer(BaseConsumer):
    stream = "health"
    group = "health-escalation"
    consumer_name = "escalation-worker"

    def handle(self, event: dict) -> None:
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "service.health":
            self._handle_health_report(event, payload)
        elif event_type == "service.degraded":
            self._escalate(event, payload)
        else:
            logger.debug("Health consumer ignoring event type: %s", event_type)

    def _handle_health_report(self, event: dict, payload: dict) -> None:
        """Process a health check report."""
        status = payload.get("status", "unknown")
        services = payload.get("services", {})

        degraded = [
            name for name, state in services.items()
            if isinstance(state, str) and state.startswith("error")
        ]

        if degraded:
            logger.warning(
                "Degraded services: %s (overall: %s)", ", ".join(degraded), status
            )
            self._escalate(event, {
                "status": status,
                "degraded_services": degraded,
                "source": event.get("source", ""),
            })
        else:
            logger.debug("All services healthy")

    def _escalate(self, event: dict, payload: dict) -> None:
        """Escalate a health issue via audit log and optional webhook."""
        degraded = payload.get("degraded_services", [])
        msg = f"Health degraded: {', '.join(degraded)}" if degraded else "Service health issue"

        log_event(
            "health.escalation",
            msg,
            category="health",
            details=payload,
            source_channel="event_bus",
        )

        webhook_url = os.environ.get("HEALTH_WEBHOOK_URL")
        if webhook_url:
            try:
                httpx.post(
                    webhook_url,
                    json={"text": msg, "services": degraded},
                    timeout=10.0,
                )
            except Exception as e:
                logger.error("Health webhook failed: %s", e)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    HealthConsumer().run()


if __name__ == "__main__":
    main()
