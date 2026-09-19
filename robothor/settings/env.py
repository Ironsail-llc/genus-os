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


def process_env_names() -> frozenset[str]:
    """Every variable name this process currently carries.

    For the callers that must ENUMERATE rather than look up: ``genus secrets
    status`` and ``genus secrets migrate`` ask "which of the names present here
    hold credentials?", a question no declared setting can answer because most
    of those names are third-party tokens the platform never declared.

    Names only. A caller that needs a value asks :func:`process_env_get` for it
    by name, which keeps "what is set?" and "what is it?" separate calls and
    means a listing can never accidentally carry a credential.
    """
    return frozenset(os.environ)


def process_env_snapshot() -> dict[str, str]:
    """A detached copy of the whole process environment.

    One caller, and it is the reason this function is not a smell:
    :func:`robothor.engine.exec_env.build_exec_env` builds a child environment
    by REJECTING from the parent's, so it has to see all of it. A copy rather
    than ``os.environ`` itself, because the result is handed to a subprocess
    and mutated on the way.
    """
    return dict(os.environ)


def process_env_allowlist(names: tuple[str, ...]) -> dict[str, str]:
    """Copy only explicitly permitted process variables into an isolated child.

    This preserves host plumbing (PATH, locale, temporary directories) without
    inheriting provider credentials, debug hooks or arbitrary browser flags.
    Declared Genus settings are resolved separately through get_settings().
    """
    return {name: os.environ[name] for name in names if name in os.environ}
