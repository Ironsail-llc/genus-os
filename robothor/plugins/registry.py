"""The signed static index a plugin can be installed FROM.

``pip install`` was the whole install story, which meant the whole supply chain
was "whatever PyPI resolves, plus whatever it depends on". This module is the
other end: a registry that is a single signed JSON file, and a set of refusals
that are the actual product.

**Why a static index rather than a package server.** A server is a thing to run,
to patch, to authenticate against and to lose. ``index.json`` plus a detached
``index.json.sig`` can be served by GitHub Pages, an S3 bucket, a company's own
nginx, or a file copied onto a box with no network at all — and every one of
those mirrors is equally trustworthy, because trust comes from the signature and
the pinned key, never from the host. That is the property a package server does
not have.

**What is signed.** The canonical form of the whole document: JSON with sorted
keys and no whitespace. The served file must already BE that form. Verifying a
re-canonicalised download instead would accept every change a JSON parser
normalises away — duplicate keys, ``1.0`` for ``1``, reordered members — so the
signature would cover the parse rather than the bytes.

**What is refused, and why each one is an attack.**

* a bad or missing signature — the whole point
* a ``key_id`` nobody pinned — otherwise the document names its own authority
* ``schema`` other than 1 — a future format read by today's rules is a guess
* ``generated_at`` more than 90 days old — a mirror that keeps serving the
  index from before a yank undoes the yank, silently
* ``generated_at`` in the future — clock games that keep a stale index alive
* a redirect off the origin — how a pinned URL becomes somebody else's URL
  without the pin ever changing
* more than 1 MB — a static index is a few hundred entries of metadata
* one name published by two different publishers — resolution that picks for
  the operator here is name-squatting with extra steps

Nothing in this module downloads a wheel or touches the filesystem;
:mod:`robothor.plugins.installer` does that, and it does it only with values
that came out of a verified index.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INDEX_URL",
    "Artifact",
    "Index",
    "IndexEntry",
    "RegistryError",
    "ScanRecord",
    "canonical_bytes",
    "configured_indexes",
    "fetch_index",
    "load_indexes",
    "parse_index",
    "publisher_keys",
    "select",
    "sign_payload",
    "signature_document",
]

#: The index schema this module speaks. A different number is refused rather
#: than read with today's rules.
SCHEMA_VERSION = 1

#: A static index is metadata for a few hundred plugins. Anything larger is
#: either not an index or is trying to be a decompression bomb.
MAX_INDEX_BYTES = 1_000_000

#: Seconds. Deliberately short: an index fetch sits in front of an operator at
#: a terminal or a 60-second HTTP handler, and a hung mirror must not own it.
INDEX_TIMEOUT_SECONDS = 10.0

#: How stale an index may be before it is refused outright. A publisher yanks a
#: compromised plugin by publishing a new index, so an old one served forever is
#: the yank being undone without anybody noticing.
MAX_INDEX_AGE_DAYS = 90

#: Tolerance for a publisher's clock running ahead of ours. Beyond it the
#: document is refused: a ``generated_at`` in the future is how a stale index is
#: kept "fresh" indefinitely.
MAX_INDEX_SKEW = timedelta(hours=24)

#: Same-origin hops a fetch will follow. Pages hosts redirect ``/x`` to
#: ``/x/``; three is plenty and an unbounded follow is a loop.
MAX_REDIRECTS = 3

#: The platform's own registry. A constant, hosted from a project repository —
#: never a personal domain, and never resolved from the operator's identity.
DEFAULT_INDEX_URL = "https://ironsail-llc.github.io/genus-plugins/index.json"

#: Verdicts a scan record may carry. Anything else is refused at parse time
#: rather than compared against later and silently treated as "not blocked".
VERDICTS = ("safe", "review", "blocked")


class RegistryError(Exception):
    """A refusal, in the sentence the operator gets told.

    Every raise site here states the reason and what to do about it. The CLI
    prints ``str(exc)`` and exits 2; the admin route turns it into a 4xx. A
    traceback out of ``genus plugin install`` helps nobody.
    """


# ---------------------------------------------------------------------------
# canonical form and signatures
# ---------------------------------------------------------------------------


def canonical_bytes(payload: Any) -> bytes:
    """The bytes that get signed: sorted keys, no whitespace, UTF-8.

    ``ensure_ascii=False`` so a non-ASCII summary is signed as the characters
    it is rather than as an escape sequence whose spelling depends on the
    encoder.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_payload(payload: Any, private_pem: str) -> str:
    """Base64 Ed25519 signature over :func:`canonical_bytes` of *payload*."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RegistryError("The signing key is not an Ed25519 private key.")
    return base64.b64encode(key.sign(canonical_bytes(payload))).decode()


def signature_document(payload: Any, private_pem: str, *, key_id: str) -> str:
    """The contents of ``index.json.sig``.

    A JSON object rather than a bare base64 blob, because a detached signature
    with no key id forces the verifier to try every pinned key -- which turns
    "this key signed it" into "some key signed it" in every log line and every
    lockfile row afterwards.
    """
    return json.dumps(
        {"key_id": key_id, "signature": sign_payload(payload, private_pem)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _verify(raw: bytes, signature_b64: str, public_pem: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = serialization.load_pem_public_key(public_pem.encode())
    except Exception:  # noqa: BLE001 - an unreadable pinned key verifies nothing
        return False
    if not isinstance(key, Ed25519PublicKey):
        return False
    try:
        key.verify(base64.b64decode(signature_b64, validate=True), raw)
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# the parsed document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """One downloadable file, pinned by hash.

    ``size`` is carried so the installer can refuse an oversized body before it
    reads it, rather than after.
    """

    kind: str
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ScanRecord:
    """What the publisher's own scanner said, at publish time.

    Advisory, and treated as such: the installer re-runs the scan on the wheel
    it actually downloaded. A verdict that only ever came from the publisher is
    the publisher marking their own homework.
    """

    verdict: str = "unscanned"
    reasons: tuple[str, ...] = ()
    scanned_at: str = ""


@dataclass(frozen=True)
class IndexEntry:
    """One published plugin version."""

    name: str
    version: str
    summary: str = ""
    homepage: str = ""
    license: str = ""
    contract_version: str = ""
    groups: tuple[str, ...] = ()
    python: str = ""
    artifacts: tuple[Artifact, ...] = ()
    manifest_sha256: str = ""
    scan: ScanRecord = field(default_factory=ScanRecord)
    yanked: bool = False
    yank_reason: str = ""

    def wheel(self) -> Artifact | None:
        for artifact in self.artifacts:
            if artifact.kind == "wheel":
                return artifact
        return None


@dataclass(frozen=True)
class Index:
    """A verified index, and where it came from."""

    url: str = ""
    schema: int = SCHEMA_VERSION
    generated_at: str = ""
    publisher_id: str = ""
    key_id: str = ""
    entries: tuple[IndexEntry, ...] = ()

    def entry(self, name: str, version: str | None = None) -> IndexEntry | None:
        """The newest matching entry, or None.

        "Newest" is the LAST matching entry in publication order rather than a
        version comparison: this module does not own a version scheme, and a
        hand-rolled comparator that gets ``1.10`` versus ``1.9`` wrong would
        install the older one while reporting the newer.
        """
        found = None
        for entry in self.entries:
            if entry.name != name:
                continue
            if version is not None and entry.version != version:
                continue
            found = entry
        return found


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"The index entry's {label} is not a string.")
    return value


def _sha256(value: Any, label: str) -> str:
    from robothor.templates.safety import TemplateSecurityError, validate_sha256

    try:
        return validate_sha256(value, label=label)
    except TemplateSecurityError as exc:
        raise RegistryError(str(exc)) from exc


def _artifact(data: Any, plugin: str) -> Artifact:
    if not isinstance(data, dict):
        raise RegistryError(f"{plugin}: an artifact entry is not an object.")
    kind = _text(data.get("kind"), "artifact kind")
    if kind != "wheel":
        raise RegistryError(
            f"{plugin}: artifact kind {kind or '(missing)'!r} is not supported; "
            "this platform installs wheels only."
        )
    filename = _text(data.get("filename"), "artifact filename")
    # The filename becomes a file on disk in the installer. Refusing a
    # separator HERE means the installer never has to decide what a slash in a
    # signed document was supposed to mean.
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise RegistryError(f"{plugin}: artifact filename {filename!r} is not a bare filename.")
    url = _text(data.get("url"), "artifact url")
    if not url.startswith("https://"):
        raise RegistryError(f"{plugin}: artifact url must be https, got {url!r}.")
    size = data.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RegistryError(f"{plugin}: artifact size is not a non-negative integer.")
    return Artifact(
        kind=kind,
        filename=filename,
        url=url,
        sha256=_sha256(data.get("sha256"), label=f"{plugin} artifact SHA-256"),
        size=size,
    )


def _scan_record(data: Any, plugin: str) -> ScanRecord:
    if data is None:
        return ScanRecord()
    if not isinstance(data, dict):
        raise RegistryError(f"{plugin}: the scan record is not an object.")
    verdict = _text(data.get("verdict"), "scan verdict")
    if verdict not in VERDICTS:
        raise RegistryError(
            f"{plugin}: scan verdict {verdict!r} is not one of {', '.join(VERDICTS)}."
        )
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise RegistryError(f"{plugin}: scan reasons are not a list of strings.")
    return ScanRecord(
        verdict=verdict,
        reasons=tuple(reasons),
        scanned_at=_text(data.get("scanned_at"), "scanned_at"),
    )


def _entry(data: Any) -> IndexEntry:
    if not isinstance(data, dict):
        raise RegistryError("The index holds a plugin entry that is not an object.")
    name = _text(data.get("name"), "name").strip()
    if not name:
        raise RegistryError("The index holds a plugin entry with no name.")
    version = _text(data.get("version"), "version").strip()
    if not version:
        raise RegistryError(f"{name}: the index entry has no version.")
    groups = data.get("groups") or []
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise RegistryError(f"{name}: groups is not a list of strings.")
    artifacts = data.get("artifacts") or []
    if not isinstance(artifacts, list) or not artifacts:
        raise RegistryError(f"{name}: the index entry lists no artifacts.")
    return IndexEntry(
        name=name,
        version=version,
        summary=_text(data.get("summary"), "summary"),
        homepage=_text(data.get("homepage"), "homepage"),
        license=_text(data.get("license"), "license"),
        contract_version=str(data.get("contract_version") or ""),
        groups=tuple(groups),
        python=_text(data.get("python"), "python"),
        artifacts=tuple(_artifact(a, name) for a in artifacts),
        manifest_sha256=_sha256(data.get("manifest_sha256"), label=f"{name} manifest SHA-256"),
        scan=_scan_record(data.get("scan"), name),
        yanked=bool(data.get("yanked")),
        yank_reason=_text(data.get("yank_reason"), "yank_reason"),
    )


def _generated_at(value: Any, now: datetime) -> str:
    raw = _text(value, "generated_at").strip()
    if not raw:
        raise RegistryError("The index has no generated_at, so its age cannot be judged.")
    try:
        # Python 3.11+ parses a trailing "Z" natively.
        stamp = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RegistryError(
            f"The index's generated_at ({raw!r}) is not an ISO-8601 timestamp."
        ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if stamp > now + MAX_INDEX_SKEW:
        raise RegistryError(
            "The index is dated in the future, which is how a stale index is "
            "kept alive. Refusing it."
        )
    age = now - stamp
    if age > timedelta(days=MAX_INDEX_AGE_DAYS):
        raise RegistryError(
            f"The index is {age.days} days old (limit {MAX_INDEX_AGE_DAYS}). A "
            "mirror serving an index from before a yank undoes the yank; ask "
            "the publisher for a current one."
        )
    return raw


def parse_index(
    raw: bytes,
    signature_raw: bytes,
    *,
    keys: dict[str, str],
    url: str = "",
    now: datetime | None = None,
) -> Index:
    """Verify and parse one index. Raises :class:`RegistryError` on any doubt.

    Order matters and is the whole design: the signature is checked against the
    bytes BEFORE anything in the document is believed, and the document's
    canonical form is compared to the bytes before that, so nothing downstream
    is ever reading a value the signature did not cover.
    """
    now = now or datetime.now(UTC)
    if not keys:
        raise RegistryError(
            "No publisher keys are pinned, so no index can be verified. Put the "
            "publisher's PEM public key in the directory named by "
            "ROBOTHOR_PLUGIN_INDEX_KEYS."
        )

    body = raw.rstrip(b"\n\r \t")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryError(f"The index is not valid JSON ({type(exc).__name__}).") from exc
    if not isinstance(payload, dict):
        raise RegistryError("The index is not a JSON object.")
    if canonical_bytes(payload) != body:
        raise RegistryError(
            "The index is not in canonical form (sorted keys, no whitespace), "
            "so the bytes served are not the bytes that were signed. Re-publish "
            "it with scripts/build_plugin_index.py."
        )

    if not signature_raw:
        raise RegistryError(
            "The index has no signature. An unsigned index is never installed from."
        )
    try:
        sig_doc = json.loads(signature_raw.decode("utf-8"))
        signature = str(sig_doc["signature"])
        sig_key_id = str(sig_doc["key_id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise RegistryError(
            "The index signature file is not a {key_id, signature} JSON object."
        ) from exc

    declared = payload.get("publisher")
    doc_key_id = str((declared or {}).get("key_id") or "") if isinstance(declared, dict) else ""
    if sig_key_id != doc_key_id:
        raise RegistryError(
            f"The signature's key_id ({sig_key_id!r}) is not the key_id the index "
            f"declares ({doc_key_id!r}). Refusing rather than picking one."
        )
    public_pem = keys.get(sig_key_id)
    if public_pem is None:
        raise RegistryError(
            f"The index is signed by key_id {sig_key_id!r}, which is not pinned on "
            "this instance. Add its PEM public key to the ROBOTHOR_PLUGIN_INDEX_KEYS "
            "directory if you trust that publisher."
        )
    if not _verify(body, signature, public_pem):
        raise RegistryError(
            "The index signature does not verify against the pinned key. The file "
            "has been modified or it was signed by a different key."
        )

    schema = payload.get("schema")
    if schema != SCHEMA_VERSION:
        raise RegistryError(
            f"The index declares schema {schema!r}; this platform reads schema "
            f"{SCHEMA_VERSION}. Upgrade Genus rather than reading a format it "
            "does not know."
        )
    generated_at = _generated_at(payload.get("generated_at"), now)

    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise RegistryError("The index has no 'plugins' list.")

    return Index(
        url=url,
        schema=SCHEMA_VERSION,
        generated_at=generated_at,
        publisher_id=_text((declared or {}).get("id"), "publisher id"),
        key_id=sig_key_id,
        entries=tuple(_entry(item) for item in plugins),
    )


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


def _same_origin(a: str, b: str) -> bool:
    left, right = urlsplit(a), urlsplit(b)
    return (left.scheme, left.hostname, left.port) == (right.scheme, right.hostname, right.port)


def bounded_get(
    client: httpx.Client,
    url: str,
    *,
    label: str,
    cap: int,
    timeout: float,
    error: type[Exception],
) -> bytes:
    """One bounded, non-redirecting GET that stays on its own origin.

    **The cap is enforced while reading, never after.** The first version did
    ``content = response.content`` and then compared its length, which meant the
    1 MB and 50 MB numbers were REPORTED rather than enforced: a hostile review
    fetched a lazily produced 300 MB body and measured a 315 MB peak allocation
    before the refusal. This runs inside the engine's admin thread, so that is a
    mirror OOM-ing the daemon through a request the operator initiated — the
    exact threat model this module opens with.

    Two gates, cheapest first: a declared ``Content-Length`` over the cap is
    refused before a byte is read, and the streamed body is counted as it
    arrives and abandoned the moment the count passes.

    Shared with :mod:`robothor.plugins.installer` rather than copied, so the
    index fetch and the artifact download cannot drift apart on the one property
    that has to hold when the mirror is not honest.
    """
    import httpx as _httpx

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with client.stream("GET", current, timeout=timeout, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    target = str(_httpx.URL(current).join(location)) if location else ""
                    if not target or not _same_origin(url, target):
                        raise error(
                            f"The {label} redirected off its origin (to "
                            f"{location or '(nothing)'!r}). A pinned URL that can be "
                            "redirected elsewhere is not pinned; refusing to follow."
                        )
                    current = target
                    continue
                if response.status_code != 200:
                    raise error(f"The {label} at {current} answered HTTP {response.status_code}.")

                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > cap:
                            raise error(
                                f"The {label} is too large: it declares {int(declared)} "
                                f"bytes, over the {cap}-byte limit."
                            )
                    except ValueError:
                        # A header that is not a number tells us nothing; the
                        # running count below is the gate that actually holds.
                        pass

                chunks: list[bytes] = []
                read = 0
                for chunk in response.iter_bytes():
                    read += len(chunk)
                    if read > cap:
                        raise error(
                            f"The {label} is too large (over the {cap}-byte limit); "
                            "the download was abandoned."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except _httpx.HTTPError as exc:
            raise error(
                f"The {label} at {current} could not be fetched ({type(exc).__name__})."
            ) from exc
    raise error(f"The {label} redirected more than {MAX_REDIRECTS} times.")


def _get(client: httpx.Client, url: str, *, label: str) -> bytes:
    return bounded_get(
        client,
        url,
        label=label,
        cap=MAX_INDEX_BYTES,
        timeout=INDEX_TIMEOUT_SECONDS,
        error=RegistryError,
    )


def fetch_index(
    url: str,
    *,
    keys: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> Index:
    """Download ``index.json`` and ``index.json.sig``, verify, and parse.

    ``client`` is injectable so the tests exercise the redirect, size and
    status rules against a transport rather than a network.
    """
    import httpx as _httpx

    if not url.startswith("https://"):
        raise RegistryError(
            f"A plugin index must be served over https; {url!r} is not. The "
            "signature protects the contents, but a plaintext fetch still leaks "
            "which plugins this instance is looking at."
        )
    pinned = publisher_keys() if keys is None else keys
    owned = client is None
    active = client or _httpx.Client(follow_redirects=False, timeout=INDEX_TIMEOUT_SECONDS)
    try:
        raw = _get(active, url, label="plugin index")
        signature = _get(active, url + ".sig", label="plugin index signature")
    finally:
        if owned:
            active.close()
    return parse_index(raw, signature, keys=pinned, url=url, now=now)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def configured_indexes() -> tuple[str, ...]:
    """Index URLs this instance reads, in order. First match wins.

    Through ``get_settings()`` rather than ``os.environ``: the env-read ratchet
    in ``tests/test_settings_registry.py`` only ever goes down, and a setting
    nobody declared is a setting an enterprise operator cannot configure.
    """
    try:
        from robothor.settings import get_settings

        raw = str(get_settings().paths.plugin_indexes or "").strip()
    except Exception as exc:  # noqa: BLE001 - settings that do not resolve are not a registry fault
        logger.debug("Plugin indexes: settings unavailable (%s)", type(exc).__name__)
        raw = ""
    if not raw:
        return (DEFAULT_INDEX_URL,)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def publisher_keys() -> dict[str, str]:
    """Every pinned publisher key: the platform's, plus the instance's.

    Never raises. A directory that does not exist, a file that is not a key and
    a permission denial all contribute nothing -- an index signed by a key that
    could not be loaded is refused by the verification path with a sentence
    naming the key id, which is a better answer than a traceback here.
    """
    from robothor.plugins.registry_keys import PUBLISHER_KEYS

    keys = dict(PUBLISHER_KEYS)
    try:
        from robothor.settings import get_settings

        directory = str(get_settings().paths.plugin_index_keys or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Plugin index keys: settings unavailable (%s)", type(exc).__name__)
        directory = ""
    if not directory:
        return keys

    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    root = Path(directory).expanduser()
    try:
        candidates = sorted(root.glob("*.pem"))
    except OSError as exc:
        logger.warning("Plugin index key directory is unreadable (%s)", type(exc).__name__)
        return keys
    for pem in candidates:
        try:
            text = pem.read_text(encoding="utf-8")
            loaded = serialization.load_pem_public_key(text.encode())
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Pinned plugin index key %s is not a readable PEM public key (%s); ignoring it",
                pem.name,
                type(exc).__name__,
            )
            continue
        if not isinstance(loaded, Ed25519PublicKey):
            logger.warning("Pinned plugin index key %s is not Ed25519; ignoring it", pem.name)
            continue
        keys[pem.stem] = text
    return keys


def load_indexes(
    urls: Sequence[str] | None = None,
    *,
    keys: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> tuple[Index, ...]:
    """Fetch every configured index. A refusal on ANY of them is a refusal.

    Deliberately not best-effort: silently skipping an index that failed
    verification means an operator whose company index was tampered with gets
    the platform's copy of a plugin instead, and nothing says so.
    """
    targets = tuple(urls) if urls is not None else configured_indexes()
    return tuple(fetch_index(url, keys=keys, client=client, now=now) for url in targets)


def select(
    name: str,
    version: str | None = None,
    *,
    indexes: Sequence[Index],
) -> tuple[IndexEntry, Index]:
    """Pick one published entry across every index, or refuse and say why.

    First index that has the name wins -- ordering is the operator's, declared
    in ``ROBOTHOR_PLUGIN_INDEXES``, so a company index listed first shadows the
    platform's on purpose. But a name offered by two DIFFERENT publishers is
    refused outright: resolving that quietly is name-squatting with the
    platform's help, and the operator is the only one who can say which of the
    two they meant.
    """
    matches = [(index, index.entry(name, version)) for index in indexes]
    found = [(index, entry) for index, entry in matches if entry is not None]
    if not found:
        where = ", ".join(index.url or "(local)" for index in indexes) or "(no index configured)"
        if version is not None:
            raise RegistryError(f"No index publishes {name} {version}. Indexes read: {where}.")
        raise RegistryError(f"No index publishes a plugin named {name!r}. Indexes read: {where}.")

    # The SIGNING KEY is the identity, not the display string beside it.
    # Comparing only ``publisher_id`` meant a second pinned publisher could
    # squat the platform's name in their own document and shadow its entry with
    # no warning at all -- and the whole point of pinning a key is that the key
    # is who the publisher is. Both are compared, and the refusal names both
    # key ids so the operator can see which pin to remove.
    identities = {(index.publisher_id, index.key_id) for index, _ in found}
    if len(identities) > 1:
        detail = ", ".join(
            f"{publisher or '(unnamed)'} signed by {key_id}"
            for publisher, key_id in sorted(identities)
        )
        raise RegistryError(
            f"{name} is published by more than one publisher ({detail}). Name the one "
            "you meant with --index <url>."
        )
    index, entry = found[0]
    assert entry is not None  # narrowed by the filter above
    if entry.yanked:
        raise RegistryError(
            f"{name} {entry.version} has been yanked by its publisher"
            + (f": {entry.yank_reason}" if entry.yank_reason else ".")
        )
    if entry.wheel() is None:
        raise RegistryError(f"{name} {entry.version} publishes no wheel artifact.")
    return entry, index
