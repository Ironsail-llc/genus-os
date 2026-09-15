"""What the operator accepted, what it looked like then, and what is turned off.

``pip install`` was the whole of plugin governance: installing a distribution
that publishes a ``genus.*`` entry point made it part of the engine, and the
only way to stop it was to uninstall the package. Three things had nowhere to
live.

* **Intent.** Nothing recorded that an operator had *decided* to run a plugin,
  so there was no difference between a capability somebody chose and one that
  arrived as a transitive dependency of something else.
* **An off switch.** Turning a plugin off to diagnose a bad turn meant
  uninstalling it and reinstalling it afterwards -- on a box where an engine
  restart cancels every in-flight run.
* **A before-picture.** :mod:`robothor.plugins.manifest` makes a distribution
  declare what it will contribute, and the loader holds it to that. But the
  declaration lives in the distribution, so an update that quietly widens it is
  self-approving. ``verify_adapter_integrity`` had already learned this for
  pinned stdio commands (``robothor/engine/adapters.py``): a hash recorded at
  the moment of acceptance is the only thing that can notice the swap.

This module is that record, and it is deliberately the smallest one that works:

* **JSON, not YAML.** It is written by a command and read by a loader on the
  boot path; a format with no ambiguity and one parser in the standard library
  is the right trade for a file nobody hand-edits.
* **Opt-in.** A distribution with no row loads exactly as it does today. A
  platform upgrade that refused every installed plugin until somebody ran a new
  command would be an outage, not a control -- and the honest consequence is
  stated rather than hidden: the lockfile constrains what it has been told
  about, so ``genus plugin sync`` is what turns it on.
* **Never fatal.** A corrupt lockfile degrades to "no lockfile" and is reported
  by ``genus doctor``. A governance file that can brick an engine gets deleted
  by the first operator it bricks, and then there is no governance at all.
* **``verdict`` is ``unscanned``.** Nothing in this task scans anything. A
  field that reads ``safe`` because no scanner ran is worse than an absent one,
  so the only value written here is the one that makes no claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DISABLED_REASON",
    "DRIFT_REASON",
    "LOCKFILE_NAME",
    "LOCKFILE_VERSION",
    "UNSCANNED",
    "LockRow",
    "Lockfile",
    "SyncResult",
    "dist_name",
    "forget_warnings",
    "lockfile_path",
    "manifest_digest",
    "read_lockfile",
    "set_enabled",
    "sync",
]

#: Filename inside the instance config directory.
LOCKFILE_NAME = "plugins.lock"

#: Schema version of the file itself. Bumped when a reader would misread an
#: older file rather than for every added field: an unknown key is ignored, so
#: additive growth costs nothing.
LOCKFILE_VERSION = 1

#: The only verdict this task writes. A scan lands in C6; until then the
#: platform has looked at nothing and says so.
UNSCANNED = "unscanned"

#: Why a plugin with ``enabled: false`` did not load. Matched exactly by the
#: doctor (which must not report a deliberate choice as a fault) and by the
#: admin API (which reports it as ``disabled``, not ``failed``), so it is a
#: constant rather than a string repeated in three places.
DISABLED_REASON = "disabled by operator"

#: Why a plugin whose manifest no longer hashes to what was recorded did not
#: load. Names the command that resolves it, because the operator reading this
#: is looking at a capability that stopped working.
DRIFT_REASON = "manifest changed since it was recorded; run genus plugin sync"

#: Paths already warned about, so a corrupt file on the boot path logs once
#: rather than on every lazy plugin lookup for the life of the process.
_warned: set[str] = set()


def forget_warnings() -> None:
    """Forget which paths have been warned about. For tests."""
    _warned.clear()


@dataclass(frozen=True)
class LockRow:
    """One distribution, as it was when the operator recorded it."""

    name: str
    version: str = ""
    manifest_sha256: str = ""
    verdict: str = UNSCANNED
    enabled: bool = True
    kinds: tuple[str, ...] = ()
    recorded_at: str = ""
    #: A hash of the distribution's own artifact. Not computed here -- an
    #: installed distribution is a directory tree, and pinning it is the
    #: install-and-scan task's problem. Empty means "not recorded", never
    #: "verified empty".
    dist_sha256: str = ""

    def as_json(self) -> dict[str, Any]:
        """The row as it is written. ``dist_sha256`` is omitted when unset so
        that an empty string never reads as a hash of nothing."""
        row: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "verdict": self.verdict,
            "enabled": self.enabled,
            "kinds": list(self.kinds),
            "recorded_at": self.recorded_at,
        }
        if self.dist_sha256:
            row["dist_sha256"] = self.dist_sha256
        return row


@dataclass(frozen=True)
class Lockfile:
    """The file's contents, plus why it might be empty.

    ``present`` and ``malformed`` are separate answers on purpose: "no lockfile
    yet" is the normal state of a fresh install and "a lockfile that does not
    parse" is a finding, and a reader that collapsed them would let a corrupted
    governance record look like an install that never had one.
    """

    path: Path | None = None
    present: bool = False
    malformed: bool = False
    rows: dict[str, LockRow] = field(default_factory=dict)

    def row(self, name: str) -> LockRow | None:
        return self.rows.get(name)

    @property
    def usable(self) -> bool:
        """Whether the loader may act on this file. A malformed or missing one
        constrains nothing -- today's behaviour, exactly."""
        return self.present and not self.malformed


@dataclass(frozen=True)
class SyncResult:
    """What one ``sync()`` changed, in the terms the operator asked in."""

    path: Path | None = None
    recorded: tuple[LockRow, ...] = ()
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def lockfile_path() -> Path | None:
    """Where the lockfile lives, or None when nothing resolves.

    The declared ``ROBOTHOR_PLUGIN_LOCKFILE`` setting wins; otherwise the file
    sits beside ``config.yaml`` in the instance config directory, which is the
    directory the platform already resolves for instance configuration. None
    only when there is no workspace at all (no ``ROBOTHOR_WORKSPACE`` and no
    home directory -- a real container case), and every caller here treats that
    as "no lockfile" rather than as an error.
    """
    try:
        from robothor.settings import get_settings

        configured = str(get_settings().paths.plugin_lockfile or "").strip()
    except Exception as exc:  # noqa: BLE001 - settings that do not resolve are not a plugin fault
        logger.debug("Plugin lockfile path: settings unavailable (%s)", type(exc).__name__)
        configured = ""
    if configured:
        return Path(configured).expanduser()

    from robothor.settings.sources import config_yaml_path

    config_file = config_yaml_path()
    return None if config_file is None else config_file.parent / LOCKFILE_NAME


def dist_name(dist: Any) -> str:
    """The distribution's name, however the metadata layer spells it.

    ``importlib.metadata.Distribution.name`` is the modern accessor and does
    not exist on every object that reaches here (a test double, an older
    backport), so the metadata mapping is the fallback rather than the other
    way round.
    """
    if dist is None:
        return ""
    name = getattr(dist, "name", None)
    if not name:
        try:
            name = (dist.metadata or {})["Name"]
        except Exception:  # noqa: BLE001 - unreadable metadata is an unnamed distribution
            name = None
    return str(name or "").strip()


def manifest_digest(dist: Any) -> str:
    """SHA-256 of the distribution's manifest, or ``""`` when it ships none.

    Read through :func:`robothor.plugins.manifest.read_manifest_text`, which is
    the same source the loader parses, so the hash and the declaration can
    never be taken from two different files.

    An absent manifest hashes to the empty string rather than to the digest of
    nothing, which is what makes "the plugin dropped its manifest" a change the
    loader notices instead of a value that happens to match.
    """
    from robothor.plugins.manifest import read_manifest_text

    try:
        raw = read_manifest_text(dist)
    except Exception:  # noqa: BLE001 - unreadable metadata is an undeclared distribution
        raw = None
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _row_from_json(data: Any) -> LockRow | None:
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    kinds = data.get("kinds")
    return LockRow(
        name=name,
        version=str(data.get("version") or ""),
        manifest_sha256=str(data.get("manifest_sha256") or ""),
        verdict=str(data.get("verdict") or UNSCANNED),
        # Anything that is not exactly ``false`` is enabled: a row whose flag
        # was hand-edited to nonsense must not silently take a capability out
        # of service, because a plugin that is off for an unreadable reason is
        # the failure mode this file exists to prevent.
        enabled=data.get("enabled") is not False,
        kinds=tuple(str(k) for k in kinds) if isinstance(kinds, list) else (),
        recorded_at=str(data.get("recorded_at") or ""),
        dist_sha256=str(data.get("dist_sha256") or ""),
    )


def read_lockfile(path: Path | None = None) -> Lockfile:
    """Read the lockfile. Never raises, never blocks boot.

    A file that does not exist, cannot be read, or does not parse all resolve
    to a :class:`Lockfile` that constrains nothing. The malformed case is
    logged once per path and reported by ``genus doctor`` -- loudly enough for
    an operator to fix, quietly enough that it cannot take the engine down.
    """
    resolved = path if path is not None else lockfile_path()
    if resolved is None:
        return Lockfile(path=None, present=False)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Lockfile(path=resolved, present=False)
    except OSError as exc:
        _warn_once(resolved, f"cannot be read ({type(exc).__name__})")
        return Lockfile(path=resolved, present=True, malformed=True)

    try:
        data = json.loads(raw)
    except ValueError as exc:
        _warn_once(resolved, f"is not valid JSON ({exc.__class__.__name__})")
        return Lockfile(path=resolved, present=True, malformed=True)

    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        _warn_once(resolved, "does not hold a 'plugins' list")
        return Lockfile(path=resolved, present=True, malformed=True)

    rows: dict[str, LockRow] = {}
    for entry in data["plugins"]:
        row = _row_from_json(entry)
        if row is not None:
            rows[row.name] = row
    return Lockfile(path=resolved, present=True, malformed=False, rows=rows)


def _warn_once(path: Path, problem: str) -> None:
    key = str(path)
    if key in _warned:
        return
    _warned.add(key)
    # The path is the instance's, so it is named as a filename rather than in
    # full -- the same rule the admin API follows.
    logger.warning(
        "Plugin lockfile %s %s — treating it as absent. Every installed plugin "
        "loads as it would with no lockfile; run `genus plugin sync` to rewrite it.",
        path.name,
        problem,
    )


def write_lockfile(rows: dict[str, LockRow], path: Path | None = None) -> Path | None:
    """Replace the lockfile in one step, readable only by its owner.

    Temp file in the same directory plus ``os.replace``: a torn write here is
    not a lost preference, it is an unparseable governance record, and the
    version a reader finds must always be a version somebody wrote whole.
    """
    resolved = path if path is not None else lockfile_path()
    if resolved is None:
        return None
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "lockfile_version": LOCKFILE_VERSION,
        "plugins": [rows[name].as_json() for name in sorted(rows)],
    }
    handle, tmp_name = tempfile.mkstemp(dir=str(resolved.parent), prefix=".plugins.lock.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")
        tmp.chmod(0o600)
        tmp.replace(resolved)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    # An existing file keeps its inode's mode through os.replace only because
    # the replacement carries its own; re-assert it so a file created before
    # this rule existed is narrowed rather than left world-readable.
    resolved.chmod(0o600)
    return resolved


def installed_distributions() -> dict[str, Any]:
    """Every distribution that publishes a ``genus.*`` entry point.

    Discovery goes through the loader's own ``_discover`` so that the lockfile
    can never record a different set of plugins from the one that loads: two
    independent walks of the entry-point registry would drift the first time a
    group was added to one of them.
    """
    from robothor.plugins import loader

    found: dict[str, Any] = {}
    for ep in loader._discover():
        dist = getattr(ep, "dist", None)
        name = dist_name(dist)
        if name:
            found.setdefault(name, dist)
    return found


def entry_point_groups() -> dict[str, set[str]]:
    """Distribution name -> the ``genus.*`` groups it publishes into."""
    from robothor.plugins import loader

    groups: dict[str, set[str]] = {}
    for ep in loader._discover():
        name = dist_name(getattr(ep, "dist", None))
        group = getattr(ep, "group", "")
        if name and group in loader._GROUPS:
            groups.setdefault(name, set()).add(group)
    return groups


def sync(path: Path | None = None) -> SyncResult:
    """Record every installed plugin distribution, keeping recorded intent.

    Upserts a row per distribution and drops rows for distributions that are no
    longer installed. ``enabled`` is carried over -- a sync must never quietly
    re-enable something an operator turned off, which is the one way this
    command could undo a decision it exists to record.
    """
    resolved = path if path is not None else lockfile_path()
    if resolved is None:
        return SyncResult(path=None)

    existing = read_lockfile(resolved).rows
    groups = entry_point_groups()
    now = datetime.now(UTC).isoformat(timespec="seconds")

    rows: dict[str, LockRow] = {}
    added: list[str] = []
    updated: list[str] = []
    for name, dist in sorted(installed_distributions().items()):
        previous = existing.get(name)
        row = LockRow(
            name=name,
            version=str(getattr(dist, "version", "") or ""),
            manifest_sha256=manifest_digest(dist),
            verdict=previous.verdict if previous else UNSCANNED,
            enabled=previous.enabled if previous else True,
            kinds=tuple(sorted(groups.get(name, set()))),
            recorded_at=now,
            dist_sha256=previous.dist_sha256 if previous else "",
        )
        rows[name] = row
        if previous is None:
            added.append(name)
        elif (
            previous.manifest_sha256 != row.manifest_sha256
            or previous.version != row.version
            or previous.kinds != row.kinds
        ):
            updated.append(name)

    removed = sorted(set(existing) - set(rows))
    write_lockfile(rows, resolved)
    return SyncResult(
        path=resolved,
        recorded=tuple(rows[name] for name in sorted(rows)),
        added=tuple(added),
        updated=tuple(updated),
        removed=tuple(removed),
    )


def set_enabled(name: str, enabled: bool, path: Path | None = None) -> LockRow | None:
    """Flip one row's ``enabled``, or None when there is no such row.

    Deliberately refuses to invent a row for a distribution nothing recorded: a
    disable that silently created a row for a typo would report success and
    change nothing, which is the shape of every inert control this platform has
    shipped. The caller turns None into "unknown plugin -- run sync".
    """
    resolved = path if path is not None else lockfile_path()
    if resolved is None:
        return None
    lock = read_lockfile(resolved)
    row = lock.rows.get(name)
    if row is None:
        return None
    updated = LockRow(
        name=row.name,
        version=row.version,
        manifest_sha256=row.manifest_sha256,
        verdict=row.verdict,
        enabled=bool(enabled),
        kinds=row.kinds,
        recorded_at=row.recorded_at,
        dist_sha256=row.dist_sha256,
    )
    rows = dict(lock.rows)
    rows[name] = updated
    write_lockfile(rows, resolved)
    return updated


def refusal_for(dist: Any, lock: Lockfile) -> str | None:
    """Why this distribution must not be imported, or None.

    The loader's gate, kept here so that the rule and the record it reads have
    one home. Order matters: a disabled plugin is reported as disabled even if
    its manifest has also drifted, because the operator's own decision is the
    more useful answer and re-syncing would not change it.
    """
    if not lock.usable:
        return None
    name = dist_name(dist)
    row = lock.rows.get(name) if name else None
    if row is None:
        return None
    if not row.enabled:
        return DISABLED_REASON
    if row.manifest_sha256 != manifest_digest(dist):
        return DRIFT_REASON
    return None
