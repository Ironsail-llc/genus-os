#!/usr/bin/env python3
"""
Chat Sync - System Cron Script
Fetches Google Chat messages and writes to chat-log.json.
Publishes events for new human messages to the event bus.
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/home/philip/robothor/brain/memory_system")
import event_bus

LOG_PATH = Path("/home/philip/robothor/brain/memory/chat-log.json")


def run_gws(args: list[str], timeout: int = 30) -> dict:
    """Run gws command and return parsed JSON or error dict."""
    try:
        proc = subprocess.run(
            ["gws"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip()[:1000] or f"gws exited with code {proc.returncode}"}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"output": proc.stdout[:4000]}
    except subprocess.TimeoutExpired:
        return {"error": f"gws command timed out after {timeout}s"}
    except FileNotFoundError:
        return {"error": "gws CLI not found"}
    except Exception as e:
        return {"error": f"gws failed: {e}"}


def load_log() -> dict:
    """Load existing log or create new one."""
    if LOG_PATH.exists():
        with open(LOG_PATH) as f:
            return json.load(f)
    return {
        "lastCheckedAt": None,
        "spaces": {},
        "lastMessageIds": {},
        "pendingMessages": [],
        "changes": [],
    }


def save_log(log: dict):
    """Save log to file."""
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def fetch_spaces() -> list[dict]:
    """Fetch all Chat spaces."""
    result = run_gws([
        "chat", "spaces", "list",
        "--params", json.dumps({"pageSize": 200}),
    ])
    if "error" in result:
        print(f"  Error fetching spaces: {result['error']}")
        return []
    return result.get("spaces", [])


def fetch_messages(space_name: str, page_size: int = 25) -> list[dict]:
    """Fetch recent messages in a space."""
    result = run_gws([
        "chat", "spaces", "messages", "list",
        "--params", json.dumps({"parent": space_name, "pageSize": page_size}),
    ])
    if "error" in result:
        print(f"  Error fetching messages for {space_name}: {result['error']}")
        return []
    return result.get("messages", [])


def is_human_sender(message: dict) -> bool:
    """Check if message is from a human (not a bot)."""
    sender = message.get("sender", {})
    return sender.get("type", "").upper() != "BOT"


def check_mentions_robothor(text: str) -> bool:
    """Check if text mentions Robothor."""
    if not text:
        return False
    return bool(re.search(r"(?i)\b@?robothor\b", text))


def check_is_question(text: str) -> bool:
    """Check if text is a question."""
    if not text:
        return False
    text_stripped = text.strip()
    if text_stripped.endswith("?"):
        return True
    question_patterns = re.compile(
        r"(?i)\b(what|when|where|who|why|how|can you|could you|would you|do you|is there|are there)\b"
    )
    return bool(question_patterns.search(text_stripped))


def main():
    print(f"[{datetime.now().isoformat()}] Chat sync starting...")

    log = load_log()
    last_message_ids = log.get("lastMessageIds", {})
    changes = log.get("changes", [])
    pending = log.get("pendingMessages", [])

    # Fetch spaces
    spaces = fetch_spaces()
    print(f"Found {len(spaces)} Chat spaces")

    spaces_map = {}
    for space in spaces:
        name = space.get("name", "")
        if not name:
            continue
        spaces_map[name] = {
            "displayName": space.get("displayName", ""),
            "type": space.get("spaceType", space.get("type", "ROOM")),
        }

    new_message_count = 0

    for space_name, space_info in spaces_map.items():
        messages = fetch_messages(space_name)
        if not messages:
            continue

        # Messages come newest-first from the API; reverse for chronological order
        messages.sort(key=lambda m: m.get("createTime", ""))

        last_seen = last_message_ids.get(space_name)
        found_last = last_seen is None  # If no last seen, all are new

        for msg in messages:
            msg_name = msg.get("name", "")

            if not found_last:
                if msg_name == last_seen:
                    found_last = True
                continue

            # Only process human messages
            if not is_human_sender(msg):
                continue

            text = msg.get("text", "")
            sender = msg.get("sender", {})
            sender_email = sender.get("name", "")  # user resource name
            sender_display = sender.get("displayName", "")
            is_dm = space_info["type"] in ("DM", "DIRECT_MESSAGE")

            mentions = check_mentions_robothor(text) or is_dm
            is_question = check_is_question(text)

            payload = {
                "space": space_name,
                "spaceName": space_info["displayName"],
                "spaceType": space_info["type"],
                "sender": sender_email,
                "senderName": sender_display,
                "text": text,
                "createTime": msg.get("createTime", ""),
                "messageName": msg_name,
                "threadName": msg.get("thread", {}).get("name", ""),
                "mentionsRobothor": mentions,
                "isQuestion": is_question,
            }

            pending.append(payload)
            event_bus.publish("chat", "chat.new_message", payload, source="chat_sync")
            new_message_count += 1

            changes.append({
                "timestamp": datetime.now().isoformat(),
                "type": "new_message",
                "space": space_name,
                "spaceName": space_info["displayName"],
                "sender": sender_display,
                "preview": text[:100] if text else "",
            })

            print(f"  New message from {sender_display} in {space_info['displayName']}")

        # Update last seen to the latest message in this space
        if messages:
            last_message_ids[space_name] = messages[-1].get("name", "")

    log["spaces"] = spaces_map
    log["lastMessageIds"] = last_message_ids
    log["pendingMessages"] = pending
    log["changes"] = changes[-100:]
    log["lastCheckedAt"] = datetime.now().isoformat()
    save_log(log)

    print(
        f"[{datetime.now().isoformat()}] Done. "
        f"{new_message_count} new messages, {len(pending)} pending."
    )


if __name__ == "__main__":
    main()
