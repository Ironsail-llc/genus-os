"""Install and remove a plugin, with every refusal ahead of every action.

Before this module the install step was ``pip install <name>`` typed by hand,
which is to say: whatever PyPI resolves today, plus its whole dependency
closure, with no signature, no hash, no scan, and no record of where any of it
came from. That is the supply chain OpenClaw lost, and it is the one this
platform had.

The pipeline is deliberately a straight line, and the ORDER is the design --
each step is a refusal, and the step that executes anything is last:

1. **resolve** — an entry in a verified index, or an explicit wheel the
   operator hashed themselves
2. **verify** — the index's Ed25519 signature against a pinned key (registry
   path only; an explicit wheel is the operator vouching in person)
3. **download** — to a temp dir, size-capped, and the sha256 must match what
   the signed index pinned
4. **open** — under :mod:`robothor.plugins.wheel`'s bounded rules: no symlink,
   no traversal, member and size caps
5. **declare** — ``genus-plugin.yaml`` must be present, and its hash must equal
   the ``manifest_sha256`` the index signed. A declaration widened after
   publication is refused HERE, rather than caught by the lockfile's drift
   check one import too late
6. **scan** — :mod:`robothor.plugins.scan`, on the bytes that were actually
   downloaded. ``blocked`` refuses; ``review`` refuses without
   ``--accept-review``; ``safe`` proceeds
7. **install** — pip, as a list, ``shell=False``
8. **record** — ``sync()``, then the row's verdict, artifact hash and source

**What pip is allowed to do: nothing.** ``--no-deps --no-index --find-links
<our temp dir>`` means pip resolves nothing, reaches nowhere, and installs
exactly the one file that survived steps 3-6. It never sees a URL, never an
``--index-url``, and never ``--pre``. A plugin that needs a dependency vendors
it or asks for a platform extra; an installer that pulls arbitrary packages on
a plugin's say-so is the hole this design exists to close, and it would close
nothing to guard the wheel and then let its metadata name the next download.

**What this does not do.** It does not restart anything. The engine keeps
serving the plugin set it discovered at boot until a SIGHUP or a reload, and
every result says so -- a control an operator believes has already applied is
worse than no control.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.plugins import registry, scan, wheel
from robothor.plugins.lockfile import LockSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    import threading
    from collections.abc import Callable, Sequence

    import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "InstallError",
    "InstallOutcome",
    "InstallPlan",
    "RELOAD_HINT",
    "install",
    "remove",
]

#: Largest wheel body this platform will download. The same number
#: ``hub_client`` uses, and two orders of magnitude above a real plugin.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

#: Seconds allowed for one artifact download.
DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: Seconds allowed for one pip invocation.
#:
#: Deliberately BELOW the engine route's own cap. It was 300 s against a 60 s
#: route cap, which meant the one step that cannot be interrupted was allowed to
#: run five times the budget -- the handler then answered "did not finish within
#: 60s" several minutes late, having let the install complete anyway.
#: ``test_pip_cannot_outlive_the_route_cap`` pins the ordering so the two
#: numbers cannot drift back apart.
PIP_TIMEOUT_SECONDS = 45.0

#: Printed and returned by everything that changes what is installed. The
#: engine re-reads the plugin set on SIGHUP (``robothor/engine/daemon.py``) or
#: on ``POST /api/plugins/reload``; nothing here signals it.
RELOAD_HINT = "reload the engine (SIGHUP) or restart to apply"


class InstallError(Exception):
    """A refusal, in the sentence the operator gets told.

    Never carries a filesystem path: the message reaches an HTTP response, and
    where this instance keeps its files is not a platform fact. The one
    exception is a path the OPERATOR typed at the CLI, echoed back so they can
    see the typo.
    """


class InstallCancelledError(InstallError):
    """The caller's deadline passed at a checkpoint. Nothing was installed.

    Raised only BEFORE pip starts, which is what makes cancellation safe: a
    cancelled install either never ran pip, or ran it and recorded its row.
    A cancel landing between "pip succeeded" and "the row was written" would
    produce exactly the state this module exists to prevent -- a distribution
    the engine will load and the lockfile has never heard of.

    A subclass of :class:`InstallError` so every caller that already handles a
    refusal handles this one too, rather than turning it into a 500.
    """


@dataclass(frozen=True)
class InstallPlan:
    """What would happen, decided before anything does.

    This is also what a ``--dry-run`` prints and what the admin route answers,
    so it is the shape the Helm's install UI reads. ``pip_command`` is the one
    field that carries paths, which is why :meth:`as_json` leaves it out unless
    the caller is the CLI.
    """

    name: str
    version: str
    origin: str = "registry"
    index_url: str = ""
    publisher_key_id: str = ""
    filename: str = ""
    sha256: str = ""
    size: int = 0
    summary: str = ""
    verdict: str = scan.SAFE
    reasons: tuple[str, ...] = ()
    prompt_scan: str = "static-only"
    groups: tuple[str, ...] = ()
    accept_review: bool = False
    #: What the scan actually looked at. Carried because "every member is
    #: accounted for" is only a check if somebody can compare the number to the
    #: wheel; a re-review found the field existed inside the scanner and reached
    #: no operator surface at all.
    files_scanned: int = 0
    members_accounted: int = 0
    pip_command: tuple[str, ...] = ()

    def as_json(self, *, include_command: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "origin": self.origin,
            "index_url": self.index_url,
            "publisher_key_id": self.publisher_key_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "summary": self.summary,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "prompt_scan": self.prompt_scan,
            "groups": list(self.groups),
            "accept_review": self.accept_review,
            "files_scanned": self.files_scanned,
            "members_accounted": self.members_accounted,
        }
        if include_command:
            payload["pip_command"] = list(self.pip_command)
        return payload


@dataclass(frozen=True)
class InstallOutcome:
    """The plan, and what became of it."""

    plan: InstallPlan
    installed: bool = False
    dry_run: bool = False
    row: dict[str, Any] | None = None
    reload_hint: str = RELOAD_HINT
    note: str = ""

    def as_json(self, *, include_command: bool = False) -> dict[str, Any]:
        return {
            "plan": self.plan.as_json(include_command=include_command),
            "installed": self.installed,
            "dry_run": self.dry_run,
            "row": self.row,
            "reload_hint": self.reload_hint,
            "note": self.note,
        }


#: What runs pip. Injectable so a test can prove the command's SHAPE without
#: installing anything into the interpreter running the tests -- the platform's
#: own venv is not a scratch directory.
PipRunner = "Callable[..., subprocess.CompletedProcess[str]]"


def _run_pip(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run pip. A LIST, ``shell=False``, bounded, output captured.

    ``shell=False`` is the default and is passed explicitly anyway: this
    command is assembled from a distribution name and a temp directory, and the
    one spelling that would make either of those dangerous is the one that is
    written out here so it cannot drift.
    """
    return subprocess.run(  # noqa: S603 - a list, shell=False, no user string reaches a shell
        command,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT_SECONDS,
        **kwargs,
    )


def _plugin_target_dir() -> str:
    """``ROBOTHOR_PLUGIN_DIR``, or "" when the interpreter's env is the target.

    Through ``get_settings()`` rather than ``os.environ``: the env-read ratchet
    only goes down.
    """
    try:
        from robothor.settings import get_settings

        return str(get_settings().paths.plugin_dir or "").strip()
    except Exception as exc:  # noqa: BLE001 - settings that do not resolve are not an install fault
        logger.debug("Plugin target dir: settings unavailable (%s)", type(exc).__name__)
        return ""


#: PEP 503's name grammar. The requirement token handed to pip is built from
#: this value, so it is validated rather than trusted.
_PEP503_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _validated_requirement(name: str, version: str) -> tuple[str, str]:
    """The distribution name and version, or a refusal naming METADATA.

    Both come straight out of the DOWNLOADED wheel's ``METADATA``, which is the
    attacker's file. Unvalidated, they reached the pip argv: a hostile review
    produced wheels whose ``Name:`` was ``--index-url=https://evil.example.org/simple``
    and whose ``Version:`` was ``1.0 --pre``, and watched them land in the
    command this module's own docstring promises never sees either. Neither was
    exploitable -- each arrives as ONE argv token, so pip consumes it as an
    option value and then errors with nothing to install -- but the invariant
    the module is built on has to be checked rather than asserted, and the check
    is two lines.

    PEP 440 through ``packaging`` rather than a regex of our own: a local
    version like ``1.0+acme1`` is legitimate and a hand-rolled pattern would
    either refuse it or admit a space.
    """
    clean_name = (name or "").strip()
    clean_version = (version or "").strip()
    if not _PEP503_NAME.match(clean_name):
        raise InstallError(
            f"The wheel's METADATA names the distribution {clean_name!r}, which is not "
            "a valid package name (PEP 503). Refusing: that value would be handed to "
            "pip as part of a requirement."
        )
    try:
        from packaging.version import InvalidVersion, Version

        Version(clean_version)
    except ImportError:  # pragma: no cover - packaging ships with pip
        if " " in clean_version or clean_version.startswith("-"):
            raise InstallError(
                f"The wheel's METADATA declares version {clean_version!r}, which is not "
                "a version. Refusing."
            ) from None
    except InvalidVersion as exc:
        raise InstallError(
            f"The wheel's METADATA declares version {clean_version!r}, which is not a "
            "valid version (PEP 440). Refusing: that value would be handed to pip as "
            "part of a requirement."
        ) from exc
    return clean_name, clean_version


def _pip_install_command(name: str, version: str, find_links: Path) -> list[str]:
    """The exact command, and nothing that could turn into another one.

    ``--no-index`` plus ``--find-links`` on OUR temp directory is what makes
    this a local install of one verified file rather than a network resolve.
    ``--no-deps`` is what keeps a plugin's metadata from naming the next
    download.
    """
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--find-links",
        str(find_links),
        "--disable-pip-version-check",
        "--no-input",
    ]
    target = _plugin_target_dir()
    if target:
        command += ["--target", target, "--upgrade"]
    command.append(f"{name}=={version}")
    return command


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def _download(url: str, *, client: httpx.Client | None) -> bytes:
    """Fetch one artifact, bounded while reading rather than after.

    Through :func:`robothor.plugins.registry.bounded_get` rather than a second
    loop that looks similar: the index fetch and this one must not drift apart
    on the property that has to hold when the mirror is hostile, and the first
    version of both buffered the whole body before checking its length -- a
    hostile review measured a 315 MB peak allocation refusing a 300 MB body
    against a 50 MB cap, inside the engine's admin thread.
    """
    import httpx as _httpx

    if not url.startswith("https://"):
        raise InstallError(f"A plugin artifact must be served over https; {url!r} is not.")
    owned = client is None
    active = client or _httpx.Client(follow_redirects=False, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        return registry.bounded_get(
            active,
            url,
            label="plugin artifact",
            cap=MAX_DOWNLOAD_BYTES,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            error=InstallError,
        )
    finally:
        if owned:
            active.close()


def _check_sha256(data: bytes, expected: str, *, what: str) -> str:
    """Hash the bytes in memory and compare. Returns the digest.

    In memory, like ``hub_client._verify_checksum``: hashing a file after
    writing it leaves a window where the thing hashed and the thing installed
    are two different reads.
    """
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected.lower():
        raise InstallError(
            f"The {what}'s sha256 is {actual}, not the {expected.lower()} that was "
            "pinned. The file served is not the file that was published; refusing."
        )
    return actual


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _looks_like_wheel(spec: str) -> bool:
    return spec.endswith(".whl") or "/" in spec or spec.startswith(("http://", "https://"))


def _split_spec(spec: str) -> tuple[str, str | None]:
    if "==" in spec:
        name, _, version = spec.partition("==")
        return name.strip(), version.strip() or None
    return spec.strip(), None


def install(
    spec: str,
    *,
    version: str | None = None,
    index: str | None = None,
    accept_review: bool = False,
    dry_run: bool = False,
    sha256: str | None = None,
    scan_prompts: bool = False,
    indexes: Sequence[str] | None = None,
    keys: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    lock_path: Path | None = None,
    pip: Callable[..., Any] | None = None,
    cancel: threading.Event | None = None,
) -> InstallOutcome:
    """Install one plugin. Every failure is an :class:`InstallError` sentence.

    ``spec`` is either a distribution name (optionally ``name==version``) to
    resolve through the configured indexes, or the path or https URL of a wheel
    -- which requires ``sha256``, because an explicit wheel has no signed index
    vouching for it and "trust me" is not a verification.

    ``cancel`` is the caller's deadline. It is checked BETWEEN pipeline steps
    and never once pip has started, which is what makes cancellation safe: a
    cancelled install either never ran pip, or ran it and recorded its row.
    There is no third state. The engine route sets this when its cap fires,
    because ``asyncio.wait_for`` around ``to_thread`` cancels the await and
    leaves the thread running -- a hostile review measured a 504 arriving 3 s
    into a 0.5 s cap with the work completing happily afterwards.
    """
    runner = pip or _run_pip
    name, inline_version = _split_spec(spec)
    version = version or inline_version

    def checkpoint(step: str) -> None:
        if cancel is not None and cancel.is_set():
            raise InstallCancelledError(
                f"The install was cancelled before {step}; nothing was installed."
            )

    checkpoint("it started")
    with tempfile.TemporaryDirectory(prefix="genus-plugin-") as staging_name:
        staging = Path(staging_name)
        if _looks_like_wheel(spec):
            plan_base, data, filename = _resolve_wheel(spec, sha256=sha256, client=client)
        else:
            plan_base, data, filename = _resolve_from_index(
                name,
                version,
                index=index,
                indexes=indexes,
                keys=keys,
                client=client,
            )

        checkpoint("the wheel was opened")
        artifact = staging / filename
        artifact.write_bytes(data)
        artifact.chmod(0o600)

        contents = _open_and_check(artifact, staging / "unpacked", plan_base)
        # BEFORE the argv, and before anything else believes these two values:
        # they came out of the downloaded wheel's METADATA, which is the
        # attacker's file.
        dist_name, dist_version = _validated_requirement(contents.name, contents.version)
        verdict = scan.scan_wheel(contents, scan_prompts=scan_prompts)

        plan = InstallPlan(
            name=dist_name,
            version=dist_version,
            origin=plan_base["origin"],
            index_url=plan_base.get("index_url", ""),
            publisher_key_id=plan_base.get("publisher_key_id", ""),
            filename=filename,
            sha256=plan_base["sha256"],
            size=len(data),
            summary=contents.summary,
            verdict=verdict.verdict,
            reasons=verdict.reasons,
            prompt_scan=verdict.prompt_scan,
            groups=contents.genus_groups(),
            accept_review=accept_review,
            files_scanned=verdict.files_scanned,
            members_accounted=verdict.members_accounted,
            pip_command=tuple(_pip_install_command(dist_name, dist_version, staging)),
        )

        _enforce_verdict(plan, accept_review=accept_review)
        # THE LAST CHECKPOINT. Past here pip runs, and pip's own timeout (below
        # the route's cap, pinned by a test) is what bounds it -- interrupting
        # between "pip succeeded" and "the row was written" would leave the
        # engine loading a distribution the lockfile has never heard of.
        checkpoint("pip ran")

        if dry_run:
            # Nothing is written and nothing is run. A dry run that left a row
            # behind, or that skipped the verdict, would be worse than none.
            return InstallOutcome(plan=plan, installed=False, dry_run=True, note="dry run")

        result = runner(list(plan.pip_command))
        if getattr(result, "returncode", 1) != 0:
            detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
            raise InstallError(
                f"pip refused to install {plan.name} {plan.version}"
                + (f": {detail.splitlines()[-1]}" if detail else ".")
            )

        row = _record(plan, contents, lock_path=lock_path)
        return InstallOutcome(
            plan=plan,
            installed=True,
            row=row,
            note=""
            if row
            else "the lockfile has no path on this instance, so nothing was recorded",
        )


def _resolve_wheel(
    spec: str, *, sha256: str | None, client: httpx.Client | None
) -> tuple[dict[str, Any], bytes, str]:
    """An explicit wheel: the operator vouches, but they vouch with a hash."""
    if not sha256:
        raise InstallError(
            "Installing a wheel directly needs --sha256 <hex>. There is no signed "
            "index vouching for a file you name yourself, so the hash is the only "
            "thing that says it is the file you meant."
        )
    from robothor.templates.safety import TemplateSecurityError, validate_sha256

    try:
        expected = validate_sha256(sha256, label="--sha256")
    except TemplateSecurityError as exc:
        raise InstallError(str(exc)) from exc

    if spec.startswith(("http://", "https://")):
        data = _download(spec, client=client)
        filename = Path(spec.split("?", 1)[0]).name or "plugin.whl"
    else:
        path = Path(spec).expanduser()
        try:
            data = path.read_bytes()
        except OSError as exc:
            # The operator typed this path, so echoing its NAME back is help,
            # not a leak: it is already on their screen.
            raise InstallError(
                f"The wheel {path.name} could not be read ({type(exc).__name__})."
            ) from exc
        filename = path.name
    if not filename.endswith(".whl"):
        raise InstallError(f"{filename!r} is not a wheel (.whl).")

    digest = _check_sha256(data, expected, what="wheel")
    return {"origin": "wheel", "sha256": digest}, data, filename


def _resolve_from_index(
    name: str,
    version: str | None,
    *,
    index: str | None,
    indexes: Sequence[str] | None,
    keys: dict[str, str] | None,
    client: httpx.Client | None,
) -> tuple[dict[str, Any], bytes, str]:
    urls: Sequence[str]
    if index:
        urls = (index,)
    elif indexes is not None:
        urls = tuple(indexes)
    else:
        urls = registry.configured_indexes()

    loaded = registry.load_indexes(urls, keys=keys, client=client)
    entry, source_index = registry.select(name, version, indexes=loaded)
    artifact = entry.wheel()
    assert artifact is not None  # select() refuses an entry with no wheel
    if artifact.size > MAX_DOWNLOAD_BYTES:
        # Refused from the SIGNED size before a byte is fetched, which is the
        # only place an oversized artifact costs nothing.
        raise InstallError(
            f"{entry.name} {entry.version} is too large: the index declares "
            f"{artifact.size} bytes, over the {MAX_DOWNLOAD_BYTES}-byte limit."
        )
    data = _download(artifact.url, client=client)
    digest = _check_sha256(data, artifact.sha256, what="wheel")
    return (
        {
            "origin": "registry",
            "sha256": digest,
            "index_url": source_index.url,
            "publisher_key_id": source_index.key_id,
            "manifest_sha256": entry.manifest_sha256,
            "entry_name": entry.name,
            "entry_version": entry.version,
        },
        data,
        artifact.filename,
    )


def _open_and_check(
    artifact: Path, unpacked: Path, plan_base: dict[str, Any]
) -> wheel.WheelContents:
    try:
        contents = wheel.open_wheel(artifact, unpacked)
    except wheel.WheelError as exc:
        raise InstallError(str(exc)) from exc

    if not contents.manifest_text:
        from robothor.plugins.manifest import MANIFEST_NAME

        raise InstallError(
            f"The wheel ships no {MANIFEST_NAME}, so nothing declares what it will "
            "contribute. An undeclared distribution is never installed."
        )

    pinned = plan_base.get("manifest_sha256")
    if pinned and contents.manifest_sha256 != pinned:
        raise InstallError(
            "The wheel's genus-plugin.yaml does not match the manifest_sha256 the "
            "index signed. Its declaration changed after publication; refusing."
        )

    expected_name = plan_base.get("entry_name")
    if expected_name and _canonical(contents.name) != _canonical(str(expected_name)):
        raise InstallError(
            f"The index published {expected_name!r} but the wheel calls itself "
            f"{contents.name!r}. Refusing rather than installing whichever one wins."
        )
    expected_version = plan_base.get("entry_version")
    if expected_version and contents.version != str(expected_version):
        raise InstallError(
            f"The index published version {expected_version!r} but the wheel is "
            f"{contents.version!r}."
        )
    return contents


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so ``acme_tools`` and ``acme-tools`` are one name."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def _enforce_verdict(plan: InstallPlan, *, accept_review: bool) -> None:
    reasons = "\n  - ".join(plan.reasons)
    if plan.verdict == scan.BLOCKED:
        raise InstallError(
            f"{plan.name} {plan.version} is blocked by the static scan and cannot be "
            f"installed:\n  - {reasons}"
        )
    if plan.verdict == scan.REVIEW and not accept_review:
        raise InstallError(
            f"{plan.name} {plan.version} needs review before it is installed:\n  - "
            f"{reasons}\nRe-run with --accept-review if you have read these and "
            "accept them."
        )


def _record(plan: InstallPlan, contents: Any, *, lock_path: Path | None) -> dict[str, Any] | None:
    """Record the install: ``sync()`` first, then this row's own facts.

    ``sync`` is what picks up everything else about the distribution the
    metadata layer can see. It is best-effort here on purpose -- it walks every
    distribution on ``sys.path`` and a broken third-party package must not turn
    a successful install into a traceback -- and :func:`record_install` writes
    the row either way.
    """
    from robothor.plugins import lockfile

    try:
        lockfile.sync(lock_path)
    except Exception as exc:  # noqa: BLE001 - discovery is not the install's verdict
        logger.warning(
            "Plugin lockfile sync after install failed (%s); recording the row directly",
            type(exc).__name__,
        )

    source = LockSource(
        origin=plan.origin,
        index_url=plan.index_url,
        publisher_key_id=plan.publisher_key_id,
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    row = lockfile.record_install(
        contents.name,
        version=contents.version,
        manifest_sha256=contents.manifest_sha256,
        kinds=contents.genus_groups(),
        verdict=plan.verdict,
        dist_sha256=plan.sha256,
        source=source,
        members_accounted=plan.members_accounted,
        path=lock_path,
    )
    return row.as_json() if row is not None else None


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def remove(
    name: str,
    *,
    force: bool = False,
    lock_path: Path | None = None,
    pip: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Uninstall a plugin this platform installed, and forget its row.

    Refuses a row with no ``source``. That row came from ``genus plugin sync``
    recording a distribution somebody pip-installed by hand, and uninstalling
    it because it appears in the platform's lockfile would be the platform
    reaching outside what it owns. ``--force`` is the operator saying they know
    which one it is.
    """
    from robothor.plugins import lockfile

    runner = pip or _run_pip
    # HERE rather than only at the two HTTP surfaces, because both callers
    # share this function and the CLI did not check: `genus plugin remove --
    # --requirement=/path/reqs.txt` reached pip as an option. Operator-typed,
    # but the guard belongs where every caller passes.
    if not _PEP503_NAME.match((name or "").strip()):
        raise InstallError(
            f"{name!r} is not a distribution name. `genus plugin list` shows what is installed."
        )
    name = name.strip()
    lock = lockfile.read_lockfile(lock_path)
    row = lock.row(name)
    if row is None and not force:
        raise InstallError(
            f"Nothing in the lockfile records {name!r}. Run `genus plugin list` to "
            "see what is installed, or `genus plugin remove --force` if you are "
            "sure the distribution is there."
        )
    if row is not None and row.source is None and not force:
        raise InstallError(
            f"{name} is recorded but this platform did not install it — there is no "
            "source on its lockfile row, so it was installed by hand. Uninstall it "
            "the same way, or re-run with --force."
        )

    result = runner([sys.executable, "-m", "pip", "uninstall", "-y", name])
    note = ""
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        lowered = detail.lower()
        if "not installed" in lowered or "skipping" in lowered:
            # pip exits non-zero for "it was not there". Keeping the row in that
            # case would leave the lockfile permanently describing something the
            # environment does not have.
            note = "pip reported the distribution was not installed; the row was dropped anyway"
        else:
            raise InstallError(
                f"pip refused to uninstall {name}"
                + (f": {detail.splitlines()[-1]}" if detail else ".")
            )

    dropped = lockfile.drop_row(name, lock_path)
    return {
        "name": name,
        "removed": True,
        "row_dropped": dropped,
        "reload_hint": RELOAD_HINT,
        "note": note,
    }
