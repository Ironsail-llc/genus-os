#!/usr/bin/env python3
"""
Robothor SMS Server — Receives and logs incoming SMS via Twilio webhook.

Endpoints:
  POST /sms        — Receive SMS from Twilio webhook
  GET  /health     — Health check
  GET  /messages   — List recent messages (JSON)
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from aiohttp import web

# Configuration
WORKSPACE = Path(__file__).parent.parent
LOG_FILE = WORKSPACE / "memory" / "sms-log.json"
MAX_LOG_ENTRIES = 1000


# Ensure log file exists
def init_log():
    """Initialize the SMS log file if it doesn't exist."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text(json.dumps([], indent=2))


def load_messages() -> list:
    """Load all messages from log file."""
    try:
        return json.loads(LOG_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_messages(messages: list):
    """Save messages to log file, keeping only the most recent entries."""
    # Keep only the most recent MAX_LOG_ENTRIES
    trimmed = messages[-MAX_LOG_ENTRIES:] if len(messages) > MAX_LOG_ENTRIES else messages
    LOG_FILE.write_text(json.dumps(trimmed, indent=2))


async def handle_sms(request):
    """Handle incoming SMS webhook from Twilio.

    Twilio sends form data with these fields:
    - MessageSid: Unique identifier for the message
    - From: Sender's phone number
    - To: Recipient's phone number (our Twilio number)
    - Body: Message text
    - NumMedia: Number of media attachments
    - MediaUrl{N}: URLs for media attachments (if any)
    """
    try:
        data = await request.post()

        # Extract key fields
        message_sid = data.get("MessageSid", "unknown")
        from_number = data.get("From", "unknown")
        to_number = data.get("To", "unknown")
        body = data.get("Body", "").strip()
        num_media = int(data.get("NumMedia", 0))

        # Collect media URLs if present
        media_urls = []
        for i in range(num_media):
            url = data.get(f"MediaUrl{i}")
            if url:
                media_urls.append(url)

        # Create message entry
        entry = {
            "receivedAt": datetime.now().isoformat(),
            "messageSid": message_sid,
            "from": from_number,
            "to": to_number,
            "body": body,
            "numMedia": num_media,
            "mediaUrls": media_urls,
            "repliedAt": None,
            "replyBody": None,
        }

        # Log to file
        messages = load_messages()
        messages.append(entry)
        save_messages(messages)

        print(f"📱 SMS from {from_number}: {body[:80]}{'...' if len(body) > 80 else ''}")

        # Respond with TwiML (empty response = no reply SMS)
        # Or we can send a confirmation message
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>✅ Received by Robothor. Philip will see this shortly.</Message>
</Response>"""

        return web.Response(text=twiml, content_type="application/xml")

    except Exception as e:
        print(f"❌ Error handling SMS: {e}")
        # Return empty TwiML on error (no SMS reply)
        return web.Response(
            text='<?xml version="1.0" encoding="UTF-8"?><Response/>', content_type="application/xml"
        )


async def handle_health(request):
    """Health check endpoint."""
    messages = load_messages()
    return web.json_response(
        {
            "status": "ok",
            "service": "robothor-sms",
            "totalMessages": len(messages),
            "logFile": str(LOG_FILE),
        }
    )


async def handle_list_messages(request):
    """List recent messages (JSON API)."""
    limit = int(request.query.get("limit", 50))
    messages = load_messages()

    # Return most recent first
    recent = list(reversed(messages[-limit:])) if messages else []

    return web.json_response(
        {
            "messages": recent,
            "total": len(messages),
            "returned": len(recent),
        }
    )


async def main(port: int):
    """Start the SMS webhook server."""
    init_log()

    app = web.Application()

    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/sms", handle_sms)
    app.router.add_get("/messages", handle_list_messages)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("📱 Robothor SMS Server")
    print(f"📍 Health:   http://localhost:{port}/health")
    print(f"📍 Webhook:  http://localhost:{port}/sms")
    print(f"📍 Messages: http://localhost:{port}/messages")
    print(f"📝 Log file: {LOG_FILE}")
    print("-" * 50)

    await asyncio.Event().wait()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Robothor SMS Server")
    parser.add_argument("--port", type=int, default=8766, help="Port to listen on")
    args = parser.parse_args()

    asyncio.run(main(args.port))
