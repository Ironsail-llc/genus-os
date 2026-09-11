"""Names a setting used to have, and what replaced them.

Duplicates are how two halves of a platform end up disagreeing about the same
value: the engine read ``ROBOTHOR_TELEGRAM_CHAT_ID`` or ``TELEGRAM_CHAT_ID``,
whichever was set first, and an operator who set the other one got silence. The
old names still work -- nothing here removes a fallback -- but reading one emits
a single ``DeprecationWarning`` per process naming the replacement, so the
duplicate shows up in the logs of the instance that still has it.

Every key must also be declared as an ``aliases=`` entry on the field that
replaced it (``tests/test_settings_registry.py`` enforces that), so a
deprecation can never point at a name the model does not know.
"""

from __future__ import annotations

import warnings

__all__ = ["DEPRECATED_ALIASES", "reset_alias_warnings", "warn_deprecated_alias"]

#: old name -> what to use instead. The replacement is normally another
#: environment variable; for settings that left the environment entirely it is
#: the file that now owns them.
DEPRECATED_ALIASES: dict[str, str] = {
    # Telegram carried two names for each value; engine/config.py read the
    # ROBOTHOR_-prefixed one first and silently fell through to the bare one.
    "TELEGRAM_BOT_TOKEN": "ROBOTHOR_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID": "ROBOTHOR_TELEGRAM_CHAT_ID",
    # Ollama had three: a full URL under two names, plus a host/port pair.
    # ROBOTHOR_OLLAMA_URL is canonical; host and port remain declared fields
    # because they are still read, but they no longer decide the endpoint when
    # a URL is present.
    "OLLAMA_URL": "ROBOTHOR_OLLAMA_URL",
    # Deployment environment: the bridge and the dashboard both prefer the
    # GENUS_ name and fall through to the ROBOTHOR_ one.
    "ROBOTHOR_ENVIRONMENT": "GENUS_ENVIRONMENT",
    # Operator identity moved out of the environment entirely. owner_config
    # already warns at its own call site; this is the registry's record of it.
    "ROBOTHOR_OWNER_NAME": "~/.robothor/owner.yaml",
    "ROBOTHOR_OWNER_EMAIL": "~/.robothor/owner.yaml",
    # The codex provider's own variable, kept working but no longer canonical.
    "CODEX_HOME": "ROBOTHOR_CODEX_HOME",
}

#: Names already warned about in this process. One warning per name per
#: process: an engine that resolves settings on every run would otherwise
#: repeat the same line until it drowned out everything else.
_warned: set[str] = set()


def reset_alias_warnings() -> None:
    """Forget which aliases have been warned about (tests only)."""
    _warned.clear()


def warn_deprecated_alias(old: str) -> None:
    """Warn once that ``old`` is deprecated, naming its replacement.

    A name that is not deprecated, or one already warned about in this
    process, does nothing.

    Stack level
    -----------
    ``stacklevel=1`` is deliberate, and it is the opposite of the usual advice.
    The caller is a settings *source*, invoked from inside pydantic-settings'
    own loop, so walking up the stack lands on
    ``pydantic_settings/main.py`` -- a vendored file the operator did not
    write, cannot grep for the variable name, and will reasonably read as a
    bug in a third-party package. Level 1 points at this file instead, which
    holds the whole deprecation table and explains itself. The message carries
    the variable and its replacement, so the location is a footnote either
    way.
    """
    replacement = DEPRECATED_ALIASES.get(old)
    if replacement is None or old in _warned:
        return
    _warned.add(old)
    warnings.warn(
        f"Configuration variable {old} is deprecated; use {replacement} "
        "instead. The old name still works, but it will stop being read "
        "after two minor releases. See robothor/settings/aliases.py.",
        DeprecationWarning,
        stacklevel=1,
    )
