"""Where settings values come from, and which source wins.

Lowest to highest: field defaults, the ``settings:`` block of
``<workspace>/.robothor/config.yaml``, the environment, then runtime overrides
passed to ``get_settings(**overrides)``. pydantic-settings applies sources
highest-priority first and deep-merges them, so a group set in config.yaml and
a single field set in the environment combine rather than replace.

Two sources are custom:

``DeclaredEnvSource``
    reads each field's *declared* environment name (and its aliases) instead of
    deriving one from the field name. Deriving is how a field called
    ``ssl_mode`` and a variable called ``ROBOTHOR_DB_SSLMODE`` stop agreeing.

``ConfigYamlSource``
    reads the ``settings:`` block verbatim, including keys the model does not
    know -- those reach validation and are rejected by name, which is the
    difference between a typo that is reported and a setting that silently
    never applied.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import PydanticBaseSettingsSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic.fields import FieldInfo

__all__ = [
    "ConfigYamlSource",
    "DeclaredEnvSource",
    "config_yaml_path",
    "owner_config_override_path",
    "workspace_path",
]

#: Directory, relative to the workspace, holding instance configuration. The
#: name is platform-hardcoded; everything inside it is instance data.
CONFIG_DIRNAME = ".robothor"
CONFIG_FILENAME = "config.yaml"

#: Top-level key in config.yaml under which settings live. Anything else in
#: that file (federation identity, for one) is not ours to validate.
SETTINGS_BLOCK = "settings"

#: Types for which an empty environment value means "unset" rather than a
#: value. ``ROBOTHOR_AI_DOMAIN=`` in an env file is how an operator blanks a
#: string; the same line against an int field would otherwise be a crash.
_EMPTY_IS_UNSET = (int, float, bool)


def workspace_path() -> Path | None:
    """The instance workspace, or None when it cannot be resolved.

    ``ROBOTHOR_WORKSPACE`` if set, else ``~/robothor``. ``Path.home()`` is only
    consulted when the variable is unset, and a ``RuntimeError`` from it (no
    HOME and no passwd entry for the running UID -- a real container case)
    yields None rather than crashing every caller.
    """
    configured = os.environ.get("ROBOTHOR_WORKSPACE")
    if configured:
        return Path(configured)
    try:
        return Path.home() / "robothor"
    except RuntimeError:
        return None


def config_yaml_path() -> Path | None:
    """Path of the instance config file, or None if no workspace resolves."""
    workspace = workspace_path()
    return None if workspace is None else workspace / CONFIG_DIRNAME / CONFIG_FILENAME


def owner_config_override_path() -> Path | None:
    """Explicit override for the operator identity file, or None.

    ``robothor.owner_config.load_owner_config()`` otherwise always resolves to
    the hardcoded ``~/.robothor/owner.yaml`` when called with no ``path``
    argument -- correct in production, but it means any real owner.yaml on the
    machine running the suite silently overrides a test's
    ``ROBOTHOR_OWNER_EMAIL`` (platform tests must never read instance data;
    see the root ``CLAUDE.md``). ``ROBOTHOR_OWNER_CONFIG`` lets a caller point
    the loader at a different file -- primarily tests, pointing it at an empty
    temp directory so the file genuinely does not exist there.

    Mirrors :func:`workspace_path`'s pattern: read here (inside
    ``robothor/settings/``, exempt from the raw-env-read-site ratchet in
    ``tests/test_settings_registry.py``) rather than in ``owner_config.py``.
    """
    configured = os.environ.get("ROBOTHOR_OWNER_CONFIG", "").strip()
    return Path(configured) if configured else None


def _declared_names(field: FieldInfo) -> tuple[str, list[str]]:
    """The primary environment name and the alias names for ``field``."""
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
    env = str(extra.get("env") or "")
    raw_aliases = extra.get("aliases")
    aliases = [str(alias) for alias in raw_aliases] if isinstance(raw_aliases, list) else []
    return env, aliases


class DeclaredEnvSource(PydanticBaseSettingsSource):
    """Read every group's fields from their declared environment names."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Unused: this source builds the whole nested mapping in __call__,
        # because the fields it reads live one level down, inside the groups.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        from pydantic import BaseModel

        from robothor.settings.aliases import warn_deprecated_alias

        values: dict[str, Any] = {}
        for group_name, group_field in self.settings_cls.model_fields.items():
            annotation = group_field.annotation
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                continue
            group_values: dict[str, Any] = {}
            for field_name, field in annotation.model_fields.items():
                env, aliases = _declared_names(field)
                for name in (env, *aliases):
                    if not name or name not in os.environ:
                        continue
                    raw = os.environ[name]
                    if raw == "" and field.annotation in _EMPTY_IS_UNSET:
                        continue
                    # No-ops unless the name that supplied the value is
                    # deprecated. A deprecated PRIMARY name counts too -- the
                    # operator identity variables are the case: they are still
                    # the only env name for the field, and owner.yaml replaced
                    # them outright.
                    warn_deprecated_alias(name)
                    group_values[field_name] = raw
                    break
            if group_values:
                values[group_name] = group_values
        return values


class ConfigYamlSource(PydanticBaseSettingsSource):
    """Read the ``settings:`` block of ``<workspace>/.robothor/config.yaml``."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = config_yaml_path()
        if path is None:
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # No config file is the normal case on a fresh install, and an
            # unreadable one must not stop a process from starting on its
            # defaults. A malformed one is a different matter -- see below.
            return {}

        import yaml

        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} is not valid YAML: {exc}") from exc
        if document is None:
            return {}
        if not isinstance(document, dict):
            raise ValueError(f"{path} must contain a mapping at the top level")
        block = document.get(SETTINGS_BLOCK)
        if block is None:
            return {}
        if not isinstance(block, dict):
            raise ValueError(f"{path}: the {SETTINGS_BLOCK!r} block must be a mapping")
        return dict(block)
