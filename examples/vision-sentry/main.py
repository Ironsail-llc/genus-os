"""
Vision Sentry Example
=====================

Runs Robothor's vision system as an intelligent security camera:
  - Motion detection via frame differencing
  - Object detection via YOLOv8
  - Face recognition via InsightFace (ArcFace)
  - Webhook alerts for unknown persons
  - Optional VLM scene analysis via Ollama

Prerequisites:
  - pip install robothor[vision]
  - A camera source (USB webcam, RTSP stream, or video file)
  - Optionally: Ollama with llama3.2-vision:11b for scene analysis

Usage:
  export RTSP_URL=rtsp://localhost:8554/webcam
  python main.py

  # Or use a USB webcam directly:
  export RTSP_URL=0
  python main.py

  # Start in armed mode (full detection, no motion gate):
  python main.py --mode armed
"""

import argparse
import asyncio
import logging
import os

from robothor.vision.alerts import AlertManager, WebhookAlert
from robothor.vision.service import VisionService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_alert_manager(webhook_url: str | None = None) -> AlertManager:
    """Create an alert manager with configured handlers.

    You can add multiple handlers here. Alerts are sent to all of them.
    Built-in handlers:
      - WebhookAlert: POST JSON to any HTTP endpoint
      - TelegramAlert: Send photos/messages via Telegram Bot API

    You can also create custom handlers by subclassing AlertHandler.
    """
    manager = AlertManager()

    # Add webhook handler if URL is provided
    webhook = webhook_url or os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        manager.add_handler(
            WebhookAlert(
                url=webhook,
                headers={"X-Source": "robothor-vision"},
            )
        )
        logger.info("Webhook alert handler configured: %s", webhook)

    # Example: Add Telegram alerts (uncomment and set env vars)
    # from robothor.vision.alerts import TelegramAlert
    # manager.add_handler(TelegramAlert(
    #     bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
    #     chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
    # ))

    if not manager.handlers:
        logger.warning(
            "No alert handlers configured. Set ALERT_WEBHOOK_URL or "
            "add handlers in build_alert_manager()."
        )

    return manager


async def main():
    """Start the vision sentry service."""
    parser = argparse.ArgumentParser(description="Vision Sentry -- intelligent security camera")
    parser.add_argument(
        "--mode",
        choices=["disarmed", "basic", "armed"],
        default="basic",
        help="Initial detection mode (default: basic)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("VISION_HEALTH_PORT", "8600")),
        help="HTTP health/control port (default: 8600)",
    )
    parser.add_argument(
        "--camera",
        type=str,
        default=os.environ.get("RTSP_URL", "rtsp://localhost:8554/webcam"),
        help="Camera URL or device index (default: rtsp://localhost:8554/webcam)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=float(os.environ.get("CAPTURE_FPS", "1.0")),
        help="Capture frames per second (default: 1.0)",
    )
    parser.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Webhook URL for alerts (or set ALERT_WEBHOOK_URL env var)",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=str,
        default=None,
        help="Directory to save detection snapshots",
    )
    parser.add_argument(
        "--face-dir",
        type=str,
        default=None,
        help="Directory for enrolled face data",
    )
    args = parser.parse_args()

    # Build alert manager with configured handlers
    alerts = build_alert_manager(webhook_url=args.webhook)

    # Create the vision service
    service = VisionService(
        rtsp_url=args.camera,
        health_port=args.port,
        default_mode=args.mode,
        capture_fps=args.fps,
        snapshot_dir=args.snapshot_dir,
        face_data_dir=args.face_dir,
        alert_manager=alerts,
    )

    # Print startup info
    print("Vision Sentry")
    print(f"  Camera: {service.rtsp_url}")
    print(f"  Mode: {service.current_mode}")
    print(f"  FPS: {service.capture_fps}")
    print(f"  Health endpoint: http://127.0.0.1:{service.health_port}/health")
    print(f"  Snapshots: {service.snapshot_dir}")
    print(f"  Face data: {service.face_data_dir}")
    print(f"  Alert handlers: {len(alerts.handlers)}")
    print()
    print("Endpoints:")
    print(f"  GET  http://127.0.0.1:{service.health_port}/health")
    print(f"  GET  http://127.0.0.1:{service.health_port}/mode")
    print(f"  POST http://127.0.0.1:{service.health_port}/mode")
    print(f"  GET  http://127.0.0.1:{service.health_port}/detections")
    print(f"  GET  http://127.0.0.1:{service.health_port}/identifications")
    print(f"  POST http://127.0.0.1:{service.health_port}/enroll")
    print(f"  POST http://127.0.0.1:{service.health_port}/look")
    print()
    print("Press Ctrl+C to stop.")
    print()

    # Run the service (blocks until Ctrl+C or SIGTERM)
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
