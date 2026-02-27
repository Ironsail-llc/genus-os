"""
Robothor Agent Engine — Python-native agent execution replacing OpenClaw.

Single daemon that handles Telegram messaging, cron scheduling,
event-driven triggers, and LLM agent execution with direct DAL calls.

Usage:
    python -m robothor.engine.daemon        # Start the engine daemon
    robothor engine run email-classifier    # Run a single agent
    robothor engine start                   # Start daemon via CLI
"""

__version__ = "0.1.0"
