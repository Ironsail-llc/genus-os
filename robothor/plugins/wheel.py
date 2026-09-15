"""Open a wheel safely, and read what it says about itself.

A wheel is a zip, and a zip handed to ``ZipFile.extractall`` is an arbitrary
write to anywhere on the filesystem. ``templates/hub_client`` already learned
this for tarballs -- bounded member count, bounded total size, no links, every
member's path contained under the destination -- and the rules here are the same
rules, applied to the zip container, using the same
:mod:`robothor.templates.safety` primitives rather than a second path validator
written from memory.

What is refused, each because it is a real shape of hostile archive:

* an absolute member path, or one with ``..`` in it -- write anywhere
* a symlink member -- write anywhere, one indirection later
* anything that is not a regular file or a directory
* two members with the same normalised path -- the second silently wins, which
  is how the file that was reviewed is not the file that is installed
* more members than :data:`MAX_WHEEL_MEMBERS`, or more uncompressed bytes than
  :data:`MAX_EXTRACTED_BYTES` -- checked BEFORE anything is written, so a bomb
  costs a header read rather than a full disk

The reader half is deliberately small: the name, version and summary from
``METADATA``, the entry-point groups from ``entry_points.txt``, and the
``genus-plugin.yaml`` the loader would later read from the installed
distribution. Nothing here imports the package, and nothing here runs a build
backend -- both would execute the code this module exists to inspect first.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from robothor.plugins.manifest import MANIFEST_NAME
from robothor.templates.safety import TemplateSecurityError, contained_path, safe_relative_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_EXTRACTED_BYTES",
    "MAX_WHEEL_BYTES",
    "MAX_WHEEL_MEMBERS",
    "WheelContents",
    "WheelError",
    "open_wheel",
    "read_wheel",
]

#: Largest wheel this platform will download or open. A plugin is a few
#: hundred kilobytes of Python; 50 MB is the same cap ``hub_client`` uses for a
#: bundle and is already generous by two orders of magnitude.
MAX_WHEEL_BYTES = 50 * 1024 * 1024

#: Members allowed in one wheel.
MAX_WHEEL_MEMBERS = 4096

#: Total uncompressed bytes allowed out of one wheel.
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024

#: The high half of a POSIX mode, as zip stores it in ``external_attr``.
_S_IFMT = 0xF000
_S_IFLNK = 0xA000
_S_IFREG = 0x8000
_S_IFDIR = 0x4000


class WheelError(Exception):
    """A refusal, in the sentence the operator gets told."""


@dataclass(frozen=True)
class WheelContents:
    """One extracted wheel, and the metadata read out of it."""

    root: Path
    dist_info: str
    name: str
    version: str
    summary: str = ""
    requires_python: str = ""
    license: str = ""
    homepage: str = ""
    #: group -> {entry point name: target}. Only what ``entry_points.txt``
    #: declares; nothing is imported to find out what it would really register.
    entry_points: dict[str, dict[str, str]] = field(default_factory=dict)
    #: The ``genus-plugin.yaml`` text, or "" when the wheel ships none. Hashing
    #: the empty string is deliberately NOT what an absent manifest gives:
    #: ``manifest_sha256`` stays empty so "no manifest" can never match an
    #: index entry that pins one.
    manifest_text: str = ""
    manifest_sha256: str = ""
    #: Every ``genus-plugin.yaml`` the wheel carries, in the order the loader's
    #: own rule would prefer them. More than one is an AMBIGUOUS declaration and
    #: the scanner blocks it: the installer pins and reviews one file while the
    #: running engine reads whichever ``read_manifest_text`` finds, and a wheel
    #: that can make those two differ has defeated the pin.
    manifest_candidates: tuple[str, ...] = ()
    #: Members whose POSIX mode carries an execute bit, read from the ZIP
    #: header. It has to be captured there: extraction narrows every member to
    #: 0600, so by the time the tree exists the evidence is gone.
    executable_members: tuple[str, ...] = ()

    def genus_groups(self) -> tuple[str, ...]:
        """The ``genus.*`` entry-point groups this wheel publishes into."""
        return tuple(sorted(g for g in self.entry_points if g.startswith("genus.")))


def _member_kind(info: zipfile.ZipInfo) -> int:
    """S_IFMT bits for a member, defaulting to "regular file".

    A zip written on Windows carries no POSIX mode at all, which is why the
    default is the permissive one -- but ``_S_IFLNK`` is checked explicitly
    below rather than inferred, so a symlink can never fall through this.
    """
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = mode & _S_IFMT
    if kind == 0:
        return _S_IFDIR if info.is_dir() else _S_IFREG
    return kind


def extract_wheel(data: bytes, destination: Path) -> dict[str, int]:
    """Extract a wheel's bytes under *destination*, bounded and contained.

    Returns each member's POSIX mode as the ZIP header declared it (0 when the
    archive carries none, which is normal for a wheel built on Windows). The
    caller needs that because extraction deliberately narrows every member to
    0600 -- so "this member shipped with the execute bit set" is a fact that
    exists only here, and the scanner has no way to recover it afterwards.

    Two passes on purpose: everything is validated against the headers first,
    and only then is a single byte written. A validate-as-you-go loop leaves a
    partially written tree behind when the tenth member turns out to be the
    hostile one.
    """
    if len(data) > MAX_WHEEL_BYTES:
        raise WheelError(f"The wheel is too large ({len(data)} bytes > {MAX_WHEEL_BYTES}).")
    destination.mkdir(parents=True, exist_ok=True)

    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos:
                raise WheelError("The wheel is empty.")
            if len(infos) > MAX_WHEEL_MEMBERS:
                raise WheelError(f"The wheel has {len(infos)} members (limit {MAX_WHEEL_MEMBERS}).")

            seen: set[str] = set()
            total = 0
            checked: list[tuple[zipfile.ZipInfo, str, bool]] = []
            for info in infos:
                kind = _member_kind(info)
                if kind == _S_IFLNK:
                    raise WheelError(
                        f"The wheel member {info.filename!r} is a symlink. A wheel that "
                        "installs a link installs whatever the link points at."
                    )
                if kind not in (_S_IFREG, _S_IFDIR):
                    raise WheelError(
                        f"The wheel member {info.filename!r} is not a regular file or directory."
                    )
                is_dir = info.is_dir()
                raw = info.filename.rstrip("/")
                if not raw:
                    continue
                try:
                    relative = safe_relative_path(raw, label="wheel member path")
                except TemplateSecurityError as exc:
                    raise WheelError(
                        f"The wheel member {info.filename!r} has an unsafe path: {exc}"
                    ) from exc
                normalized = PurePosixPath(*relative.parts).as_posix()
                if normalized in seen:
                    raise WheelError(f"The wheel holds two members named {normalized!r}.")
                seen.add(normalized)
                if not is_dir:
                    total += info.file_size
                    if total > MAX_EXTRACTED_BYTES:
                        raise WheelError(
                            f"The wheel unpacks to more than {MAX_EXTRACTED_BYTES} bytes "
                            "(uncompressed size)."
                        )
                # Containment is re-checked against the real destination for
                # each parent as it is created, below; here it validates the
                # shape before anything exists.
                checked.append((info, normalized, is_dir))

            modes: dict[str, int] = {}
            for info, member, is_dir in checked:
                target = destination / PurePosixPath(member)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                modes[member] = (info.external_attr >> 16) & 0o7777
                target.parent.mkdir(parents=True, exist_ok=True)
                # Re-resolve through the containment helper now that the parent
                # exists: a directory created earlier in this same archive
                # cannot be a symlink (we refuse those), but the helper is what
                # the static analyser recognises as the sanitizer.
                try:
                    safe_target = contained_path(destination, member, label="wheel extraction path")
                except TemplateSecurityError as exc:
                    raise WheelError(f"The wheel member {member!r} escapes its root.") from exc
                with archive.open(info) as source, safe_target.open("xb") as out:
                    written = 0
                    while chunk := source.read(1 << 16):
                        written += len(chunk)
                        if written > info.file_size:
                            raise WheelError(
                                f"The wheel member {member!r} is larger than its header declares."
                            )
                        out.write(chunk)
                safe_target.chmod(0o600)
            return modes
    except zipfile.BadZipFile as exc:
        raise WheelError(f"The wheel is not a readable zip archive ({exc}).") from exc
    except OSError as exc:
        raise WheelError(f"The wheel could not be extracted ({type(exc).__name__}).") from exc


def _parse_entry_points(text: str) -> dict[str, dict[str, str]]:
    """``entry_points.txt`` as ``{group: {name: target}}``.

    Hand-parsed rather than through ``importlib.metadata``: that API wants an
    installed distribution, and the whole point of this module is to look at
    the wheel BEFORE it is one.
    """
    groups: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            groups.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        name, _, target = line.partition("=")
        groups[current][name.strip()] = target.strip()
    return groups


def read_wheel(
    root: Path, *, dist_info: str | None = None, modes: dict[str, int] | None = None
) -> WheelContents:
    """Read an already-extracted wheel tree. Never imports anything.

    ``modes`` are the ZIP header's POSIX modes, from :func:`extract_wheel`.
    Without them the executable-member rule cannot fire, because extraction
    has already narrowed every file to 0600.
    """
    if dist_info is None:
        candidates = sorted(p.name for p in root.iterdir() if p.name.endswith(".dist-info"))
        if not candidates:
            raise WheelError("The wheel has no .dist-info directory, so it is not a wheel.")
        if len(candidates) > 1:
            raise WheelError(
                f"The wheel holds {len(candidates)} .dist-info directories; exactly one is a wheel."
            )
        dist_info = candidates[0]

    info_dir = root / dist_info
    try:
        metadata_text = (info_dir / "METADATA").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise WheelError(f"The wheel's METADATA could not be read ({type(exc).__name__}).") from exc
    meta = Parser().parsestr(metadata_text)

    name = str(meta.get("Name") or "").strip()
    version = str(meta.get("Version") or "").strip()
    if not name or not version:
        raise WheelError("The wheel's METADATA names no distribution or no version.")

    homepage = str(meta.get("Home-page") or "").strip()
    if not homepage:
        for url in meta.get_all("Project-URL") or []:
            label, _, value = str(url).partition(",")
            if label.strip().lower() in ("homepage", "home"):
                homepage = value.strip()
                break

    entry_points: dict[str, dict[str, str]] = {}
    ep_file = info_dir / "entry_points.txt"
    if ep_file.is_file():
        entry_points = _parse_entry_points(ep_file.read_text(encoding="utf-8", errors="replace"))

    # THE LOADER'S OWN RULE, not a second search that happens to look similar.
    # ``manifest.read_manifest_text`` tries ``<dist-info>/genus-plugin.yaml``
    # first and only then walks the distribution's files; this used to take the
    # first ``rglob`` hit instead. A wheel carrying two manifests could
    # therefore get one file pinned by the index and reviewed by the scan while
    # the running engine enforced the OTHER -- the drift check caught the
    # divergence at load time, but the headline property ("the manifest the
    # index signed is the declaration this engine holds the plugin to") was not
    # actually established. Every candidate is carried so the scanner can
    # refuse the ambiguity outright.
    manifests: list[Path] = []
    preferred = info_dir / MANIFEST_NAME
    if preferred.is_file() and not preferred.is_symlink():
        manifests.append(preferred)
    for found in sorted(root.rglob(MANIFEST_NAME)):
        if found == preferred or found.is_symlink() or not found.is_file():
            continue
        manifests.append(found)
    manifest_text = manifests[0].read_text(encoding="utf-8", errors="replace") if manifests else ""

    return WheelContents(
        root=root,
        dist_info=dist_info,
        name=name,
        version=version,
        summary=str(meta.get("Summary") or "").strip(),
        requires_python=str(meta.get("Requires-Python") or "").strip(),
        license=str(meta.get("License-Expression") or meta.get("License") or "").strip(),
        homepage=homepage,
        entry_points=entry_points,
        manifest_text=manifest_text,
        # An absent manifest hashes to "" rather than to the digest of nothing,
        # so it can never accidentally equal an index entry's pin.
        manifest_sha256=(
            hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() if manifest_text else ""
        ),
        manifest_candidates=tuple(m.relative_to(root).as_posix() for m in manifests),
        executable_members=tuple(
            sorted(member for member, mode in (modes or {}).items() if mode & 0o111)
        ),
    )


def open_wheel(path: Path, destination: Path) -> WheelContents:
    """Extract one wheel file under *destination* and read it."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WheelError(
            f"The wheel {path.name} could not be read ({type(exc).__name__})."
        ) from exc
    modes = extract_wheel(data, destination)
    return read_wheel(destination, modes=modes)
