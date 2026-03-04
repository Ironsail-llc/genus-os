#!/usr/bin/env python3
"""
Real-time transcript watcher using inotify.
Watches Moltbot session files and syncs to PostgreSQL on changes.
"""

import subprocess
import sys
import time
from pathlib import Path

try:
    import inotify.adapters
except ImportError:
    print("Installing inotify...")
    subprocess.run([sys.executable, "-m", "pip", "install", "inotify"], check=True)
    import inotify.adapters

SESSIONS_DIR = Path.home() / ".moltbot" / "agents" / "main" / "sessions"
SCRIPT_DIR = Path(__file__).parent
SYNC_SCRIPT = SCRIPT_DIR / "transcript_sync.py"
VENV_PYTHON = SCRIPT_DIR / "venv" / "bin" / "python"

# Debounce: don't sync more than once per N seconds
MIN_SYNC_INTERVAL = 5
last_sync_time = 0


def sync_transcripts():
    """Run the transcript sync."""
    global last_sync_time

    now = time.time()
    if now - last_sync_time < MIN_SYNC_INTERVAL:
        return  # Debounce

    last_sync_time = now

    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(SYNC_SCRIPT), "--no-vectorize", "--limit", "1"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(SCRIPT_DIR),
        )
        if result.stdout.strip():
            print(f"[{time.strftime('%H:%M:%S')}] {result.stdout.strip()}")
        if result.stderr.strip():
            print(f"[{time.strftime('%H:%M:%S')}] ERROR: {result.stderr.strip()}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[{time.strftime('%H:%M:%S')}] Sync timed out", file=sys.stderr)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Sync error: {e}", file=sys.stderr)


def watch():
    """Watch session directory for changes."""
    print(f"Watching: {SESSIONS_DIR}")
    print(f"Sync script: {SYNC_SCRIPT}")
    print(f"Min interval: {MIN_SYNC_INTERVAL}s")
    print("---")

    # Ensure directory exists
    if not SESSIONS_DIR.exists():
        print(f"Creating sessions directory: {SESSIONS_DIR}")
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    i = inotify.adapters.Inotify()
    i.add_watch(str(SESSIONS_DIR))

    print(f"[{time.strftime('%H:%M:%S')}] Watcher started")

    try:
        for event in i.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event

            # Only care about JSONL file modifications
            if not filename.endswith(".jsonl"):
                continue

            if "IN_MODIFY" in type_names or "IN_CLOSE_WRITE" in type_names:
                print(f"[{time.strftime('%H:%M:%S')}] Change detected: {filename}")
                sync_transcripts()

    except KeyboardInterrupt:
        print("\nWatcher stopped")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Real-time transcript watcher")
    parser.add_argument("--test", action="store_true", help="Test sync once and exit")
    args = parser.parse_args()

    if args.test:
        print("Testing sync...")
        sync_transcripts()
        return

    watch()


if __name__ == "__main__":
    main()
