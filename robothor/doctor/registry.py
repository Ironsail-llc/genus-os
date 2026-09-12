"""Which checks exist: the built-in set, plus whatever plugins contribute.

Built-ins are collected by importing the modules in
:mod:`robothor.doctor.checks` and reading each module's ``CHECKS`` tuple. There
is deliberately no decorator-based registry and no hand-maintained list of ids:
a second list beside the thing it describes is the drift that produced three
separate "hardcoded names" defects on this instance, and a ``CHECKS`` tuple in
the module that defines them cannot fall behind them.

Plugin checks arrive through the ``genus.doctor`` entry-point group, whose
payload key is ``checks``. Two rules apply on top of everything
:mod:`robothor.plugins.loader` already enforces, and both are refusals rather
than warnings:

* **every id must be prefixed with the plugin's own distribution name.** An
  operator reading a red line in ``genus doctor`` has to be able to tell whose
  check it is, and a doctor is precisely where a third party could otherwise
  make an authoritative-looking claim about the host.
* **no id may shadow a built-in.** Replacing ``db.migrations`` with a check
  that always passes is not an extension.

A plugin that breaks either rule has ALL of its checks skipped, not just the
offending one, and the refusal is logged: a half-registered plugin is a state
its author never tested.
"""

from __future__ import annotations

import importlib
import logging
import re
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_MODULES",
    "PLUGIN_GROUP",
    "all_checks",
    "builtin_checks",
    "builtin_ids",
    "categories",
    "plugin_checks",
]

#: The entry-point group a plugin publishes doctor checks under. Declared in
#: ``robothor/plugins/loader.py``; named here so the two can be asserted equal.
PLUGIN_GROUP = "genus.doctor"

#: One module per category, imported in the order the report prints them:
#: configuration first (nothing else is meaningful if settings do not load),
#: then the stores, then the things that use them, then the host.
CHECK_MODULES = (
    "config",
    "database",
    "redis",
    "models",
    "channels",
    "services",
    "manifests",
    "identity",
    "secrets",
    "host",
)


def builtin_checks() -> tuple[Check, ...]:
    """Every check that ships with the platform.

    Imports are done here rather than at module import so that a category whose
    module is broken cannot take the whole doctor with it -- the failure is
    logged and the rest still run, which is the same all-or-nothing-per-source
    rule the plugin loader applies.
    """
    collected: list[Check] = []
    seen: set[str] = set()
    for name in CHECK_MODULES:
        try:
            module = importlib.import_module(f"robothor.doctor.checks.{name}")
        except Exception as exc:  # noqa: BLE001 - one broken category is not ten
            logger.warning("doctor: check module %r failed to import: %s", name, exc)
            continue
        for check in getattr(module, "CHECKS", ()):
            if check.id in seen:
                logger.warning("doctor: duplicate built-in check id %r in %r", check.id, name)
                continue
            seen.add(check.id)
            collected.append(check)
    return tuple(collected)


def builtin_ids() -> frozenset[str]:
    """The ids a plugin may not take."""
    return frozenset(check.id for check in builtin_checks())


def categories() -> tuple[str, ...]:
    """Every category name, in report order, deduplicated."""
    ordered: list[str] = []
    for check in builtin_checks():
        if check.category not in ordered:
            ordered.append(check.category)
    return tuple(ordered)


def _normalise(name: str) -> str:
    """Distribution names and check-id segments, in one spelling.

    ``genus-widget``, ``genus_widget`` and ``Genus.Widget`` are the same
    project; PEP 503 says so for the first two and the third is what a check id
    would naturally use. Comparing raw strings would refuse a correctly-named
    plugin for punctuation.
    """
    return re.sub(r"[-_.]+", "_", name).strip().lower()


def plugin_checks(entry_points: Sequence[Any] | None = None) -> tuple[Check, ...]:
    """Checks contributed by installed plugins, after the two id rules.

    ``entry_points`` is injectable for the suite; the default consults the real
    registry. Each entry point is loaded ON ITS OWN through
    :func:`robothor.plugins.loader.load_plugins`, which is what makes the
    all-or-nothing rule per-plugin rather than per-process -- and which keeps
    the contract-version gate, the manifest gate and the reserved-name refusal
    in one place instead of reimplemented here.
    """
    from robothor.plugins.loader import load_plugins

    if entry_points is None:
        entry_points = _discover()

    reserved = builtin_ids()
    collected: list[Check] = []
    claimed: set[str] = set()
    for entry_point in entry_points:
        if getattr(entry_point, "group", "") != PLUGIN_GROUP:
            continue
        dist = getattr(getattr(entry_point, "dist", None), "name", "") or ""
        loaded = load_plugins(entry_points=[entry_point], reserved_names=set(reserved))
        for failure in loaded.failures:
            logger.warning(
                "doctor: plugin %r contributed no checks: %s", failure.name, failure.reason
            )
        contributed = dict(loaded.doctor)
        if not contributed:
            continue

        prefix = f"{_normalise(dist)}_" if dist else ""
        refusals = [
            check_id
            for check_id in contributed
            if not prefix or not _normalise(check_id).startswith(prefix)
        ]
        if refusals:
            logger.warning(
                "doctor: plugin %r offered check id(s) %s that are not prefixed with its "
                "distribution name %r — refusing all of its checks",
                getattr(entry_point, "name", "<unnamed>"),
                sorted(refusals),
                dist,
            )
            continue

        wrong_type = [
            check_id
            for check_id, check in contributed.items()
            if not isinstance(check, Check) or check.id != check_id
        ]
        if wrong_type:
            logger.warning(
                "doctor: plugin %r offered %s under a key that is not a Check with that id "
                "— refusing all of its checks",
                getattr(entry_point, "name", "<unnamed>"),
                sorted(wrong_type),
            )
            continue

        taken = sorted(set(contributed) & claimed)
        if taken:
            logger.warning(
                "doctor: plugin %r offered check id(s) %s another plugin already claimed "
                "— refusing all of its checks",
                getattr(entry_point, "name", "<unnamed>"),
                taken,
            )
            continue

        claimed |= set(contributed)
        collected.extend(contributed[key] for key in sorted(contributed))
    return tuple(collected)


def _discover() -> list[Any]:
    from importlib import metadata

    try:
        return list(metadata.entry_points(group=PLUGIN_GROUP))
    except Exception as exc:  # noqa: BLE001 - a broken distribution must not stop the doctor
        logger.warning("doctor: plugin discovery failed: %s", exc)
        return []


def all_checks(entry_points: Sequence[Any] | None = None) -> tuple[Check, ...]:
    """Built-ins first, then plugins. The order is the report order."""
    return builtin_checks() + plugin_checks(entry_points)
