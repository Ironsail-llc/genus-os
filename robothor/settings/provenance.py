"""Where a setting's effective value actually came from.

``get_settings()`` answers "what is this set to?". It cannot answer "who set
it?" -- and an operator debugging a box needs the second question far more
often than the first, because the usual defect is not a wrong value but a value
arriving from a layer nobody remembered: a variable in
``/etc/robothor/robothor.env`` overriding the config file someone just edited,
or a default that looks like a decision.

This module re-walks the same precedence the sources implement -- environment
above config.yaml above the declared default -- and reports which layer won.
It lives here, beside the sources it mirrors, rather than in the CLI: the raw
``os.environ`` reads it needs are the one place in the platform where reading
the environment directly IS the correct behaviour, and keeping them next to
``sources.py`` means the precedence is described once.

Governed flags have a fourth, higher layer -- an operator-written row in
``feature_flags`` -- which the caller resolves; see ``robothor/cli/config_cmd``.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "SOURCE_FILE",
    "SOURCE_RUNTIME",
    "deprecated_names_in_use",
    "effective_value",
    "env_name_in_use",
    "file_value",
    "resolve",
    "running_env_value",
    "settings_block",
]

#: Provenance labels, highest precedence first.
SOURCE_RUNTIME = "runtime"
SOURCE_ENV = "env"
SOURCE_FILE = "config.yaml"
SOURCE_DEFAULT = "default"


def settings_block() -> dict[str, Any]:
    from robothor.settings.sources import _read_settings_block

    try:
        return _read_settings_block()
    except ValueError:
        # A malformed config file is reported by `genus config validate`, with
        # the filename; provenance should not be the thing that raises.
        return {}


def file_value(record: dict[str, Any]) -> tuple[bool, Any]:
    """``(present, value)`` for this field in the config.yaml settings block.

    Any spelling the source accepts counts: the Python field name, the declared
    environment name, or a deprecated alias.
    """
    group = settings_block().get(record["group"])
    if not isinstance(group, dict):
        return False, None
    field = record["field"].split(".", 1)[1]
    for key in (field, record["env"], *record["aliases"]):
        if key in group:
            return True, group[key]
    return False, None


def env_name_in_use(record: dict[str, Any]) -> str | None:
    """The environment name actually supplying this field, if any.

    Same order the environment source reads: the declared name first, then each
    alias, so a mid-migration box that sets both is reported under the name
    that wins rather than the one it still has lying around.
    """
    for name in (record["env"], *record["aliases"]):
        if os.environ.get(name, "") != "":
            return str(name)
    return None


def running_env_value(name: str) -> str | None:
    """The raw value this process was started with, or None."""
    return os.environ.get(name)


def deprecated_names_in_use() -> list[tuple[str, str]]:
    """``(old, replacement)`` for every deprecated name set in this process."""
    from robothor.settings.aliases import DEPRECATED_ALIASES

    return [
        (old, new) for old, new in sorted(DEPRECATED_ALIASES.items()) if os.environ.get(old, "")
    ]


def effective_value(record: dict[str, Any]) -> Any:
    """The typed value the platform reads, resolved through the model."""
    from robothor.settings import get_settings

    group, field = record["field"].split(".", 1)
    return getattr(getattr(get_settings(), group), field)


def resolve(record: dict[str, Any]) -> tuple[Any, str, str]:
    """``(value, source, detail)`` for one declared field.

    The value is always the typed one the platform would read; the source names
    the layer that supplied it and the detail says exactly where -- a variable
    name (with a note when it is a deprecated one), or the path of the file.
    """
    name = env_name_in_use(record)
    if name is not None:
        detail = name if name == record["env"] else f"{name} (deprecated name)"
        return effective_value(record), SOURCE_ENV, detail

    present, _raw = file_value(record)
    if present:
        from robothor.settings.sources import config_yaml_path

        return effective_value(record), SOURCE_FILE, str(config_yaml_path())
    return effective_value(record), SOURCE_DEFAULT, "declared default"
