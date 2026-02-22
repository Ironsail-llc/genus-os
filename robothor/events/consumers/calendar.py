"""
Calendar consumer — processes calendar sync events.

On calendar.sync events, detects conflicts, cancellations, and changes.
Can trigger the Calendar Monitor agent for conflict resolution.

Run as: python -m robothor.events.consumers.calendar
"""

from __future__ import annotations

import logging
import os
import subprocess

from robothor.events.consumers.base import BaseConsumer

logger = logging.getLogger(__name__)


class CalendarConsumer(BaseConsumer):
    stream = "calendar"
    group = "calendar-monitor"
    consumer_name = "monitor-worker"

    def handle(self, event: dict) -> None:
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "calendar.conflict":
            self._handle_conflict(event, payload)
        elif event_type == "calendar.cancellation":
            self._handle_cancellation(event, payload)
        elif event_type == "calendar.change":
            self._handle_change(event, payload)
        else:
            logger.debug("Calendar consumer ignoring event type: %s", event_type)

    def _handle_conflict(self, event: dict, payload: dict) -> None:
        """Handle a calendar conflict event."""
        events = payload.get("events", [])
        logger.warning("Calendar conflict detected: %d overlapping events", len(events))
        self._trigger_monitor(event)

    def _handle_cancellation(self, event: dict, payload: dict) -> None:
        """Handle a meeting cancellation."""
        title = payload.get("title", "Unknown meeting")
        logger.info("Meeting cancelled: %s", title)
        self._trigger_monitor(event)

    def _handle_change(self, event: dict, payload: dict) -> None:
        """Handle a calendar event change (time, attendees, etc.)."""
        title = payload.get("title", "Unknown meeting")
        change = payload.get("change_type", "unknown")
        logger.info("Calendar change (%s): %s", change, title)

    def _trigger_monitor(self, event: dict) -> None:
        """Trigger the Calendar Monitor script if configured."""
        monitor_script = os.environ.get("CALENDAR_MONITOR_SCRIPT")
        if monitor_script and os.path.exists(monitor_script):
            try:
                subprocess.Popen(  # noqa: S603
                    ["python3", monitor_script],
                    cwd=os.path.dirname(monitor_script),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("Triggered calendar monitor")
            except Exception as e:
                logger.error("Failed to trigger calendar monitor: %s", e)
                raise


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    CalendarConsumer().run()


if __name__ == "__main__":
    main()
