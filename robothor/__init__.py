"""Genus OS — An AI brain with persistent memory, vision, and self-healing."""

from robothor.settings.third_party import pin_litellm_offline as _pin_litellm_offline

# Before any module can import litellm: see robothor/settings/third_party.py.
_pin_litellm_offline()

__version__ = "1.100.1"
