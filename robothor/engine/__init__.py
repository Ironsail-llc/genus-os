"""
Genus OS Agent Engine — Python-native agent execution engine.

Single daemon that handles Telegram messaging, cron scheduling,
event-driven triggers, and LLM agent execution with direct DAL calls.

Usage:
    python -m robothor.engine.daemon        # Start the engine daemon
    robothor engine run email-classifier    # Run a single agent
    robothor engine start                   # Start daemon via CLI
"""

from robothor import __version__ as __version__
