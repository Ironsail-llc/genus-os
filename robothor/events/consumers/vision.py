"""
Vision consumer — processes vision detection events.

On vision.detection events, handles alerts for unknown persons,
motion events, and VLM analysis results.

Run as: python -m robothor.events.consumers.vision
"""

from __future__ import annotations

import logging
import os

import httpx

from robothor.audit.logger import log_event
from robothor.events.consumers.base import BaseConsumer

logger = logging.getLogger(__name__)


class VisionConsumer(BaseConsumer):
    stream = "vision"
    group = "vision-alerts"
    consumer_name = "alert-worker"

    def handle(self, event: dict) -> None:
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "vision.person_detected":
            self._handle_person(event, payload)
        elif event_type == "vision.motion":
            self._handle_motion(event, payload)
        elif event_type == "vision.analysis":
            self._handle_analysis(event, payload)
        else:
            logger.debug("Vision consumer ignoring event type: %s", event_type)

    def _handle_person(self, event: dict, payload: dict) -> None:
        """Handle a person detection event."""
        name = payload.get("name", "unknown")
        confidence = payload.get("confidence", 0)
        is_known = payload.get("is_known", False)

        if not is_known:
            logger.warning("Unknown person detected (confidence: %.2f)", confidence)
            self._alert(f"Unknown person detected (confidence: {confidence:.0%})", payload)
        else:
            logger.info("Known person detected: %s", name)

    def _handle_motion(self, event: dict, payload: dict) -> None:
        """Handle a motion detection event."""
        zone = payload.get("zone", "unknown")
        logger.debug("Motion in zone: %s", zone)

    def _handle_analysis(self, event: dict, payload: dict) -> None:
        """Handle a VLM scene analysis result."""
        description = payload.get("description", "")
        if description:
            logger.info("Scene analysis: %s", description[:100])

    def _alert(self, message: str, payload: dict) -> None:
        """Send an alert for a vision event."""
        log_event(
            "vision.alert",
            message,
            category="vision",
            details=payload,
            source_channel="event_bus",
        )

        webhook_url = os.environ.get("VISION_WEBHOOK_URL")
        if webhook_url:
            try:
                httpx.post(
                    webhook_url,
                    json={"text": message, "event": payload},
                    timeout=10.0,
                )
            except Exception as e:
                logger.error("Vision webhook failed: %s", e)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    VisionConsumer().run()


if __name__ == "__main__":
    main()
