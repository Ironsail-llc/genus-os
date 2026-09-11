"""Typed access to every Genus OS setting.

    from robothor.settings import get_settings

    settings = get_settings()
    settings.engine.max_concurrent_agents
    settings.database.name

The model itself lives in :mod:`robothor.settings.model`; the traversal the
docs and the CLI use is in :mod:`robothor.settings.registry`.

Import cost
-----------
This module imports nothing from pydantic at import time. ``pydantic_settings``
pulls pydantic's full machinery, and ``genus --help`` has no business paying
for it -- so the model is imported inside :func:`get_settings`, and
``tests/test_settings_registry.py`` asserts in a subprocess that importing
``robothor.cli`` leaves ``pydantic_settings`` out of ``sys.modules``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.settings.model import GenusSettings

__all__ = ["get_settings", "reset_settings"]


@lru_cache(maxsize=1)
def _cached_settings() -> GenusSettings:
    from robothor.settings.model import GenusSettings

    return GenusSettings()


def get_settings(**overrides: Any) -> GenusSettings:
    """Resolve the instance's settings.

    Without arguments the result is cached for the process: resolution reads
    a file and the whole environment, and the engine asks for settings far too
    often to repeat that.

    With ``overrides`` -- ``get_settings(engine={"max_concurrent_agents": 1})``
    -- a fresh object is built with those values at the top of the precedence
    stack and is deliberately NOT cached, so a caller passing a one-off
    override cannot poison what every other caller sees.

    Raises:
        pydantic.ValidationError: if config.yaml or the environment holds a
            value the model rejects, or a key it does not know. The error names
            the key.
    """
    if not overrides:
        return _cached_settings()
    from robothor.settings.model import GenusSettings

    return GenusSettings(**overrides)


def reset_settings() -> None:
    """Drop the cached settings so the next call re-reads its sources.

    For tests and for a future SIGHUP reload. Deprecation warnings already
    emitted are not reset; see ``robothor.settings.aliases``.
    """
    _cached_settings.cache_clear()
