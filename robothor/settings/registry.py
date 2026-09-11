"""Walk :class:`~robothor.settings.model.GenusSettings` as data.

Three consumers need the model as a flat list of declarations rather than as a
class: the guard test (does a declaration exist for every name the platform
reads?), the documentation generator, and the ``genus config`` commands that
come next. They all read it from here so there is one traversal, not three that
drift.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic.fields import FieldInfo

__all__ = ["declared_env_names", "field_index", "groups"]


def _group_models() -> dict[str, type[BaseModel]]:
    from robothor.settings.model import GenusSettings

    found: dict[str, type[BaseModel]] = {}
    for name, field in GenusSettings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found[name] = annotation
    return found


def _extra(field: FieldInfo) -> dict[str, Any]:
    extra = field.json_schema_extra
    return dict(extra) if isinstance(extra, dict) else {}


@lru_cache(maxsize=1)
def groups() -> tuple[str, ...]:
    """Group names, in declaration order."""
    return tuple(_group_models())


@lru_cache(maxsize=1)
def field_index() -> dict[str, dict[str, Any]]:
    """Every declared environment name mapped to its declaration.

    Both the primary name and each alias are keys, pointing at the same
    record, so a lookup works whichever name an operator actually set. The
    record carries: ``group``, ``field`` (the Python attribute path),
    ``env`` (the primary name), ``aliases``, ``default``, ``type``,
    ``description``, ``restart_required``, ``secret``, ``since``, ``governed``.

    Raises:
        ValueError: if two fields claim the same primary environment name.
            Two fields for one variable is the duplicate this package exists
            to make impossible, so it fails at import rather than resolving to
            whichever was declared last.
    """
    index: dict[str, dict[str, Any]] = {}
    primaries: dict[str, str] = {}
    for group_name, model in _group_models().items():
        for field_name, field in model.model_fields.items():
            extra = _extra(field)
            env = extra.get("env")
            if not env:
                raise ValueError(
                    f"{group_name}.{field_name} declares no env name; use "
                    "robothor.settings.model.declare()"
                )
            path = f"{group_name}.{field_name}"
            if env in primaries:
                raise ValueError(f"{env} is declared twice: {primaries[env]} and {path}")
            primaries[env] = path
            annotation = field.annotation
            record = {
                "group": group_name,
                "field": path,
                "env": env,
                "aliases": list(extra.get("aliases") or []),
                "default": field.default,
                "type": getattr(annotation, "__name__", str(annotation)),
                "description": field.description or "",
                "restart_required": bool(extra.get("restart_required", True)),
                "secret": bool(extra.get("secret", False)),
                "since": extra.get("since") or "legacy",
                "governed": bool(extra.get("governed", False)),
            }
            index[env] = record
            for alias in record["aliases"]:
                index.setdefault(alias, record)
    return index


@lru_cache(maxsize=1)
def declared_env_names() -> frozenset[str]:
    """Every environment name the platform declares, aliases included."""
    return frozenset(field_index())
