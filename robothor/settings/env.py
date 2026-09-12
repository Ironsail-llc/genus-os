"""The one place dynamic-named environment variables are read or written.

Every *declared* setting is read through :func:`robothor.settings.get_settings`.
A handful of names cannot be declared as fields because they are generated at
runtime — provider key slots such as ``OPENROUTER_API_KEY_3`` — and litellm
resolves credentials from the process environment, so the key pool must also
write them. Those reads and writes go through here, so the settings package
stays the single owner of the process environment and the env-read ratchet in
``tests/test_settings_registry.py`` keeps counting call sites elsewhere.
"""

from __future__ import annotations

import os


def process_env_get(name: str, default: str | None = "") -> str | None:
    """Read a dynamically named variable (a provider key slot) from the process."""
    return os.environ.get(name, default)


def process_env_set(name: str, value: str) -> None:
    """Publish a dynamically named variable so libraries reading the process see it."""
    os.environ[name] = value


def process_env_unset(name: str) -> None:
    """Remove a dynamically named variable; absent is not an error."""
    os.environ.pop(name, None)
