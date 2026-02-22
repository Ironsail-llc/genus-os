"""
Email pipeline consumer — processes new email events from the event bus.

On email.new events, triggers the email classification and response pipeline.
Falls back to cron-based processing if Redis is unavailable.

Run as: python -m robothor.events.consumers.email
"""

from __future__ import annotations

import logging
import os
import subprocess

from robothor.events.consumers.base import BaseConsumer

logger = logging.getLogger(__name__)


class EmailConsumer(BaseConsumer):
    stream = "email"
    group = "email-pipeline"
    consumer_name = "pipeline-worker"

    def handle(self, event: dict) -> None:
        event_type = event.get("type", "")
        payload = event.get("payload", {})

        if event_type == "email.new":
            self._process_new_email(event, payload)
        elif event_type == "email.classified":
            self._process_classified(event, payload)
        else:
            logger.debug("Email consumer ignoring event type: %s", event_type)

    def _process_new_email(self, event: dict, payload: dict) -> None:
        """Trigger email classification for a new email."""
        email_id = payload.get("email_id", payload.get("id", "unknown"))
        subject = payload.get("subject", "")
        logger.info("Processing new email: %s — %s", email_id, subject[:80])

        # Trigger the email hook pipeline if configured
        hook_script = os.environ.get("EMAIL_HOOK_SCRIPT")
        if hook_script and os.path.exists(hook_script):
            try:
                subprocess.Popen(  # noqa: S603
                    ["python3", hook_script],
                    cwd=os.path.dirname(hook_script),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "EMAIL_EVENT_ID": str(email_id)},
                )
                logger.info("Triggered email hook for: %s", email_id)
            except Exception as e:
                logger.error("Failed to trigger email hook: %s", e)
                raise  # Don't ack on failure
        else:
            logger.info("No EMAIL_HOOK_SCRIPT configured, event logged only")

    def _process_classified(self, event: dict, payload: dict) -> None:
        """Handle a classified email event (from the classifier agent)."""
        email_id = payload.get("email_id", "unknown")
        classification = payload.get("classification", "unknown")
        logger.info("Email classified: %s → %s", email_id, classification)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    EmailConsumer().run()


if __name__ == "__main__":
    main()
