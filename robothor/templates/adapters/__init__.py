"""
Format adapters — pluggable converters for different agent runtime formats.

Currently only robothor_native is implemented. Others (docker_cagent, n8n,
crewai) will be added when the Agent Hub needs them.
"""

from __future__ import annotations

__all__ = ["get_adapter", "FormatAdapter"]


def get_adapter(format_id: str = "robothor-native/v1"):
    """Get a format adapter by ID."""
    if format_id.startswith("robothor-native"):
        from robothor.templates.adapters.robothor_native import RobothorNativeAdapter

        return RobothorNativeAdapter()
    raise ValueError(f"Unknown format: {format_id}. Available: robothor-native/v1")
