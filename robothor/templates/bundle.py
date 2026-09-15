"""The agent-bundle envelope — what one instance hands another.

A template bundle (``setup.yaml`` + ``manifest.template.yaml`` + instructions)
already existed as the thing the hub publishes and the installer renders. What
did not exist was a way to hand one to somebody: no statement of what the agent
NEEDS on the far side, no integrity over the bundle's own files, and no check
that an operator exporting their agent is not also exporting their API key.

``bundle.yaml`` is that statement, and this module is its parser, its integrity
check and its two export gates. It deliberately holds no filesystem policy of
its own beyond :mod:`robothor.templates.safety` — every path in a bundle is an
untrusted relative path and is parsed as one.

**The file list covers the whole directory, not just what it names.** A
``files[]`` that lists three of four members is the interesting attack: the
fourth is then a payload nothing vouched for, riding inside a bundle whose
hashes all "match". :func:`verify_bundle_files` therefore walks the directory
and refuses an unlisted member as loudly as a tampered one.

**The two gates are refusals, not repairs.** A credential found in an export is
not redacted and shipped — redacting it would produce a bundle that installs and
silently does not work, and would teach an operator that the tool cleans up
after them. It is a hard failure naming ``file:line``, and nothing is written.
Same for an instance-specific absolute path: ``/home/<someone>/robothor`` in a
skill is the leak gate's own rule, applied at the moment the file leaves.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from robothor.secrets.redaction import redact
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    safe_relative_path,
    trusted_directory,
    validate_identifier,
    validate_sha256,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

__all__ = [
    "BUNDLE_FILENAME",
    "BUNDLE_KIND",
    "BUNDLE_SCHEMA",
    "BundleError",
    "BundleFile",
    "BundleManifest",
    "Finding",
    "Requires",
    "bundle_document",
    "credential_shape",
    "file_digest",
    "parse_bundle",
    "parse_requires",
    "read_bundle",
    "scan_instance_leaks",
    "scan_secret_literals",
    "verify_bundle_files",
]

#: The control file every agent bundle carries at its root.
BUNDLE_FILENAME = "bundle.yaml"

#: What ``kind`` an agent bundle declares. The signed plugin index carries the
#: same string, so a wheel and a bundle can never be mistaken for one another.
BUNDLE_KIND = "agent-bundle"

#: The envelope version this module reads. A different number is refused rather
#: than read with today's rules.
BUNDLE_SCHEMA = 1

#: Requirement lists a bundle may declare. A fifth key is a newer producer's
#: idea that this reader would silently drop, so it is refused instead.
REQUIRE_KINDS = ("plugins", "adapters", "secrets", "skills")

#: A bundle is a handful of text files. Anything past this is not a bundle.
MAX_BUNDLE_FILES = 256


class BundleError(Exception):
    """A refusal, in the sentence the operator gets told."""


# ---------------------------------------------------------------------------
# the two export gates
# ---------------------------------------------------------------------------

#: ``${NAME}`` — how a bundle is SUPPOSED to name a credential. Substituted out
#: before the credential scan runs, so the reference a bundle should contain is
#: never mistaken for the value it must not.
_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]{0,63}\}")

#: ``Bearer ${TOKEN}`` needs its own pass. :mod:`robothor.secrets.redaction`
#: matches ``Bearer\\s+\\S+`` — ANY non-space run after the word — so replacing
#: the placeholder with any word at all still leaves a credential-shaped header
#: behind and every adapter that authenticates correctly would fail its export.
#: Collapsing the space is what takes the shape away.
_BEARER_REFERENCE = re.compile(r"(?i)\bbearer\s+\$\{[A-Za-z_][A-Za-z0-9_]{0,63}\}")

#: Credential shapes recognised by their VALUE, wherever the value sits — in a
#: URL, inside a ``command:`` array, in the middle of an English sentence.
#:
#: This list exists because the first cut of this gate was
#: :func:`robothor.secrets.redaction.redact` plus a key-name rule, and a hostile
#: review exported a GitHub PAT, a GitLab PAT, an AWS access key, a Google API
#: key, a PEM private key and a password embedded in a URL — cleanly, with the
#: literals verbatim in the published bundle. ``redact`` is not at fault: its
#: job is keeping a credential out of a LOG LINE this platform is writing, and
#: it is deliberately narrow so an operator can still read their own error. A
#: bundle is a distribution artefact going to a stranger, and the cost of a
#: false positive here is one refused export with a sentence naming the line.
#: Different job, different list.
#:
#: Every entry is a PREFIXED, self-identifying token family — the kind that can
#: be recognised without guessing at entropy — plus the two structural shapes
#: (URL userinfo, PEM armour) that mean "credential" by construction.
_VALUE_SHAPES: tuple[tuple[str, str], ...] = (
    # ``scheme://user:password@host``. The password half must be present: a bare
    # ``ssh://git@host`` is a username and is left alone. Neither half may
    # contain ``/``, so ``https://h:8080/a@b`` is a port and a path, not a
    # credential.
    (r"://[^/\s:@]{1,256}:[^/\s@]{1,256}@", "credentials embedded in a URL"),
    (r"\bgh[pousr]_[A-Za-z0-9]{16,}", "a GitHub token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}", "a GitHub fine-grained token"),
    (r"\bglpat-[A-Za-z0-9_-]{16,}", "a GitLab token"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{15,16}\b", "an AWS access key id"),
    # No trailing ``\b``: a Google key is base64url and may end in ``-``, after
    # which a word boundary does not fire — so the anchored form silently
    # dropped a whole class of real keys. ``{35,}`` is greedy and takes the
    # whole run, which is the same thing a lookahead would buy with less to
    # read.
    (r"\bAIza[0-9A-Za-z_-]{35,}", "a Google API key"),
    (r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----", "a PEM private key"),
    (r"\bnpm_[A-Za-z0-9]{36}\b", "an npm token"),
    (r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b", "a Shopify token"),
    # A JSON Web Token: three base64url runs. The header segment is anchored on
    # ``eyJ`` (``{"`` in base64) so an ordinary hyphenated identifier with two
    # dots in it cannot match.
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}", "a JSON Web Token"),
)

_CREDENTIAL_SHAPED = tuple((re.compile(pattern), reason) for pattern, reason in _VALUE_SHAPES)

#: Names that mean "credential" wherever one appears. Used for the ``key: value``
#: form, which :func:`redact` deliberately does not cover: its rule is ``=`` and
#: only ``=``, because ``TOKEN: expected`` in an English sentence is a sentence.
#: A bundle is not prose, though — it is YAML — so ``api_key: hunter2`` inside an
#: adapter is exactly the literal this gate exists to catch, and the ``=`` rule
#: would walk straight past it.
#:
#: ``[_-]PAT`` with a letter lookahead rather than a bare ``PAT``: ``github_pat``
#: is a credential, and ``path``, ``patch``, ``pattern`` and — the one that
#: actually fired on the shipped corpus — ``instruction_file_path`` are not.
_CREDENTIAL_NAME = (
    r"(?:SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|CREDENTIALS"
    r"|API[_-]?KEY|ACCESS[_-]?KEY|SECRET[_-]?KEY|PRIVATE[_-]?KEY|AUTH[_-]?KEY"
    r"|TOKEN|BOT[_-]?TOKEN|CLIENT[_-]?SECRET|[_-]PAT(?![A-Za-z]))"
)

#: Trailing words that turn a credential name into a name ABOUT credentials.
#: ``token_path`` is a filename, ``max_tokens`` a count, ``secret_backend`` a
#: choice of vault. Borrowed in spirit from ``redaction._ABOUT_NOT_THE_THING``;
#: ``ID`` is deliberately NOT here, because ``access_key_id`` carrying a literal
#: AWS key is one of the cases this gate was widened for.
_ABOUT_NOT_THE_THING = (
    r"(?:PATH|FILE|DIR|NAME|ENV|VAR|COUNT|LEN|LENGTH|ENABLED|DISABLED|TTL|SIZE"
    r"|PREFIX|SUFFIX|FORMAT|ALG|ALGO|ALGORITHM|SOURCE|BACKEND|PROVIDER|HEADER"
    r"|REQUIRED|POOL|MODE|COLUMN|FIELD|ORDER|TYPE|S)"
)

#: One ``name: value`` line whose name means credential and whose value is a
#: literal. ``null``, ``~``, an empty string and a bare env reference are all
#: "no value here", which is what a correctly exported bundle looks like.
#:
#: A value that OPENS A COLLECTION is excluded too, and that one is not a
#: nicety: ``requires.secrets: [BILLING_API_KEY]`` is the bundle's own list of
#: variable NAMES, so a rule that read it as a credential would make every
#: correctly declared bundle unexportable. A credential is a scalar — no
#: provider has ever issued a list.
#:
#: The credential word need only APPEAR in the key name, not terminate it:
#: ``access_key_id:`` walked straight past a rule that required the word at the
#: end. What follows it is bounded by the deny-list above so ``token_path`` and
#: ``max_tokens`` do not become credentials by acquiring a suffix.
_MAPPING_CREDENTIAL = re.compile(
    rf"^\s*(?:-\s*)?[\"']?[A-Za-z0-9_-]{{0,40}}{_CREDENTIAL_NAME}"
    rf"(?:[_-]?(?!{_ABOUT_NOT_THE_THING}\b)[A-Za-z0-9]{{1,20}}){{0,2}}[\"']?\s*:\s*"
    r"(?![\"']?(?:null|~|EnvRef|BearerEnvRef|true|false)[\"']?\s*$)"
    r"(?![\[{|>&*#])"
    r"[\"']?\S",
    re.IGNORECASE,
)

#: A credential stated in prose: ``the billing password is hunter2hunter2``.
#:
#: Three narrowings, and each one is a false positive this rule produced:
#:
#: 1. **The noun must be SINGULAR and DETERMINED** — "the password is",
#:    "my api key is". "Passwords are argon2id, minimum 12 characters", copied
#:    verbatim from this repo's own ``docs/configuration.md``, made an agent
#:    unexportable. A statement about credentials in general is a policy; a
#:    statement about *the* credential is a leak.
#: 2. **The value must not be a known algorithm or format name.** ``argon2id``,
#:    ``sha256`` and ``ed25519`` all satisfy "letters and digits", and all three
#:    are the answer to "what is the password *hashed with*".
#: 3. **The value must be long enough to be one.** Twelve characters, not eight:
#:    at eight, ordinary technical prose keeps landing on it.
#:
#: Instruction files are prose, and prose about authentication is what operators
#: write most, so this rule is the one most likely to be met in practice. It
#: earns a refusal only when the sentence is carrying the value itself.
_ALGORITHM_NAMES = (
    r"(?:argon2[a-z]*|bcrypt|scrypt|pbkdf2[a-z0-9]*|sha\d+[a-z-]*|md5|hmac[a-z0-9-]*"
    r"|ed25519[a-z-]*|rsa\d*|ecdsa[a-z0-9-]*|aes\d*[a-z-]*|base64|utf-?8|oauth\d?|jwt"
    r"|tls\d*[a-z.]*|ssl\d*)"
)

_PROSE_CREDENTIAL = re.compile(
    r"(?i)\b(?:the|this|that|our|my|its|his|her|their|a|an)\s+"
    r"(?:[a-z0-9_-]{1,20}\s+){0,3}"
    r"(?:password|passphrase|passwd|secret|api[ _-]?key|access[ _-]?key|token|credential)"
    r"\s+(?:is|was)\s+[\"']?"
    rf"(?!{_ALGORITHM_NAMES}\b)"
    r"(?=[A-Za-z0-9+/=_.-]{12,})(?=[A-Za-z0-9+/=_.-]*\d)(?=[A-Za-z0-9+/=_.-]*[A-Za-z])"
    r"[A-Za-z0-9+/=_.-]{12,}"
)

#: The leak gate's own patterns (``scripts/check_instance_leak.py``), applied at
#: the moment a file leaves the instance rather than at the moment it is
#: committed. A bundle is published just as surely as a commit is.
_LEAK_PATTERNS = (
    (re.compile(r"/home/\w+/"), "a hardcoded home path"),
    (re.compile(r"/Users/\w+/"), "a hardcoded home path"),
    (re.compile(r"/root/\w"), "a hardcoded root path"),
)


@dataclass(frozen=True)
class Finding:
    """One refusal, located but never quoted.

    ``reason`` says what kind of thing was found; the text that triggered it is
    NOT carried. An error message that helpfully echoes the credential it found
    publishes it to the terminal, the shell history and whatever captured the
    command's output — which is the same failure this gate exists to prevent.
    """

    path: str
    line: int
    reason: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _neutralize_env_references(text: str) -> str:
    """Replace ``${NAME}`` references with inert words.

    Order matters: the ``Bearer`` form collapses its own whitespace first, so
    the generic substitution below cannot leave ``Bearer <word>`` behind.
    """
    return _ENV_REFERENCE.sub("EnvRef", _BEARER_REFERENCE.sub("BearerEnvRef", text))


def credential_shape(value: str) -> str | None:
    """The name of the credential family *value* carries, or None.

    Exposed because the exporter needs the same answer when it decides which
    adapter values to collapse to ``${NAME}``. One list, asked twice: a second
    opinion about what a credential looks like is how ``--include-adapters``
    shipped three of them while reporting ``requires.secrets: []``.
    """
    neutral = _neutralize_env_references(value)
    for pattern, reason in _CREDENTIAL_SHAPED:
        if pattern.search(neutral):
            return reason
    if redact(neutral) != neutral:
        return "a credential-shaped literal"
    return None


def scan_secret_literals(text: str, path: str) -> list[Finding]:
    """Every line of *text* that carries a credential VALUE rather than a reference.

    Four rules, in decreasing confidence:

    1. :data:`_VALUE_SHAPES` — self-identifying token families and the two
       structural shapes (URL userinfo, PEM armour). These need no context at
       all, which is why they catch a key inside a ``command:`` array.
    2. :func:`redact` — the shapes this platform already knows, asked rather
       than re-implemented, so the two cannot drift.
    3. :data:`_MAPPING_CREDENTIAL` — a credential-named YAML key with a literal
       value, the form redaction deliberately leaves alone in prose.
    4. :data:`_PROSE_CREDENTIAL` — "the password is <something value-shaped>",
       which is how a credential ends up in an instruction file's prose.
    """
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        neutral = _neutralize_env_references(line)
        shaped = next(
            (reason for pattern, reason in _CREDENTIAL_SHAPED if pattern.search(neutral)),
            None,
        )
        if shaped is not None:
            findings.append(Finding(path, number, shaped))
        elif redact(neutral) != neutral:
            findings.append(Finding(path, number, "a credential-shaped literal"))
        elif _MAPPING_CREDENTIAL.search(neutral):
            findings.append(Finding(path, number, "a credential-named field with a literal value"))
        elif _PROSE_CREDENTIAL.search(neutral):
            findings.append(Finding(path, number, "a credential stated in prose"))
    if findings:
        return findings

    # The multi-line shape (an SMTP ``AUTH LOGIN`` exchange pasted into a
    # runbook) spans lines, so no single line ever differs. Whole-text pass,
    # attributed to the file rather than guessed at a line.
    neutral_text = _neutralize_env_references(text)
    if text and redact(neutral_text) != neutral_text:
        return [Finding(path, 1, "a credential-shaped literal spanning several lines")]
    return []


def scan_instance_leaks(text: str, path: str, *, workspace: str = "") -> list[Finding]:
    """Every line of *text* carrying a path that only exists on this instance.

    *workspace* is the exporting instance's own root. It is passed in rather
    than read here because a bundle built in a temporary directory must be
    judged against the workspace it came FROM, not against wherever it is
    being assembled.
    """
    patterns = list(_LEAK_PATTERNS)
    absolute_workspace = workspace.rstrip("/")
    if absolute_workspace.startswith("/"):
        patterns.append(
            (re.compile(re.escape(absolute_workspace)), "this instance's workspace path")
        )

    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in patterns:
            if pattern.search(line):
                findings.append(Finding(path, number, reason))
                break
    return findings


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleFile:
    """One member of a bundle, pinned by hash."""

    path: str
    sha256: str


@dataclass(frozen=True)
class Requires:
    """What the far side must already have for this agent to run.

    "Present or not" — nothing here resolves a version or installs anything.
    An operator who can see the list can act on it; an installer that guessed
    would be choosing an instance's plugin set on its behalf.
    """

    plugins: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def as_document(self) -> dict[str, list[str]]:
        return {kind: list(getattr(self, kind)) for kind in REQUIRE_KINDS}

    def is_empty(self) -> bool:
        return not any(getattr(self, kind) for kind in REQUIRE_KINDS)


@dataclass(frozen=True)
class BundleManifest:
    """A parsed, structurally valid ``bundle.yaml``."""

    id: str
    name: str
    version: str
    exported_at: str = ""
    platform_version: str = ""
    requires: Requires = Requires()
    files: tuple[BundleFile, ...] = ()

    def file_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.files)


def _text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BundleError(f"The bundle's {label} is not a string.")
    return value


def _require_list(value: Any, kind: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BundleError(f"The bundle's requires.{kind} is not a list of strings.")
    return tuple(value)


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def parse_requires(value: Any) -> Requires:
    if value is None:
        return Requires()
    if not isinstance(value, dict):
        raise BundleError("The bundle's requires block is not a mapping.")
    unknown = sorted(set(value) - set(REQUIRE_KINDS))
    if unknown:
        raise BundleError(
            f"The bundle's requires block declares {', '.join(unknown)}, which this "
            "platform does not know how to satisfy. Upgrade Genus rather than "
            "installing a bundle whose requirements it cannot read."
        )
    parsed = {kind: _require_list(value.get(kind), kind) for kind in REQUIRE_KINDS}
    for name in parsed["secrets"]:
        if _ENV_NAME.fullmatch(name) is None:
            raise BundleError(
                f"requires.secrets holds {name!r}, which is not an environment "
                "variable NAME. A bundle names the variable, never its value."
            )
    return Requires(**parsed)


def _parse_files(value: Any) -> tuple[BundleFile, ...]:
    if not isinstance(value, list) or not value:
        raise BundleError("The bundle lists no files, so nothing about it is pinned.")
    if len(value) > MAX_BUNDLE_FILES:
        raise BundleError(
            f"The bundle lists {len(value)} files, over the {MAX_BUNDLE_FILES} limit."
        )
    seen: set[str] = set()
    parsed: list[BundleFile] = []
    for item in value:
        if not isinstance(item, dict):
            raise BundleError("A bundle file entry is not a mapping.")
        try:
            relative = safe_relative_path(item.get("path"), label="bundle file path")
        except TemplateSecurityError as exc:
            raise BundleError(str(exc)) from exc
        normalized = PurePosixPath(*relative.parts).as_posix()
        if normalized == BUNDLE_FILENAME:
            raise BundleError(
                f"{BUNDLE_FILENAME} cannot list itself; it is the list, not a member of it."
            )
        if normalized in seen:
            raise BundleError(f"The bundle lists {normalized} twice.")
        seen.add(normalized)
        try:
            digest = validate_sha256(item.get("sha256"), label=f"{normalized} SHA-256")
        except TemplateSecurityError as exc:
            raise BundleError(str(exc)) from exc
        parsed.append(BundleFile(path=normalized, sha256=digest))
    return tuple(parsed)


def parse_bundle(data: Any) -> BundleManifest:
    """Validate a loaded ``bundle.yaml`` into a :class:`BundleManifest`.

    Structure only. Whether the files it names are actually there and actually
    hash to what it claims is :func:`verify_bundle_files`, which needs a
    directory; keeping the two apart means a plan can be shown for a bundle
    before a byte of it is trusted.
    """
    if not isinstance(data, dict):
        raise BundleError(f"{BUNDLE_FILENAME} is not a mapping.")

    kind = _text(data.get("kind"), "kind")
    if kind != BUNDLE_KIND:
        raise BundleError(
            f"This is not an {BUNDLE_KIND}: it declares kind {kind or '(missing)'!r}. "
            "A plugin wheel and an agent bundle are installed by different verbs."
        )
    schema = data.get("schema")
    if schema != BUNDLE_SCHEMA:
        raise BundleError(
            f"The bundle declares schema {schema!r}; this platform reads schema "
            f"{BUNDLE_SCHEMA}. Upgrade Genus rather than reading a format it does not know."
        )
    try:
        agent_id = validate_identifier(data.get("id"), label="bundle agent ID")
    except TemplateSecurityError as exc:
        raise BundleError(str(exc)) from exc
    version = _text(data.get("version"), "version").strip()
    if not version:
        raise BundleError("The bundle has no version.")

    return BundleManifest(
        id=agent_id,
        name=_text(data.get("name"), "name") or agent_id,
        version=version,
        exported_at=_text(data.get("exported_at"), "exported_at"),
        platform_version=_text(data.get("platform_version"), "platform_version"),
        requires=parse_requires(data.get("requires")),
        files=_parse_files(data.get("files")),
    )


def bundle_document(manifest: BundleManifest) -> str:
    """``bundle.yaml``'s text, in one fixed key order.

    Fixed because an export has to be reproducible: the same agent must produce
    the same bytes, and a mapping serialized in whatever order a dict happened
    to have is the easiest way to lose that.
    """
    document: dict[str, Any] = {
        "kind": BUNDLE_KIND,
        "schema": BUNDLE_SCHEMA,
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "exported_at": manifest.exported_at,
        "platform_version": manifest.platform_version,
        "requires": manifest.requires.as_document(),
        "files": [{"path": entry.path, "sha256": entry.sha256} for entry in manifest.files],
    }
    return yaml.dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True)


def read_bundle(directory: str | Path) -> BundleManifest:
    """Load and parse the ``bundle.yaml`` at the root of *directory*."""
    try:
        root = trusted_directory(directory, label="bundle directory")
        control = contained_path(root, BUNDLE_FILENAME, label="bundle manifest path")
    except TemplateSecurityError as exc:
        raise BundleError(str(exc)) from exc
    if not control.is_file():
        raise BundleError(
            f"There is no {BUNDLE_FILENAME} here, so this is a template directory rather "
            "than an agent bundle. Export it with 'genus agent export' first."
        )
    try:
        loaded = yaml.safe_load(control.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BundleError(f"{BUNDLE_FILENAME} could not be read ({type(exc).__name__}).") from exc
    return parse_bundle(loaded)


def file_digest(path: Path) -> str:
    """SHA-256 of one file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> Iterable[tuple[str, Path]]:
    """Every member under *root* except the control file, as (relative, path)."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BundleError(
                f"The bundle member {path.relative_to(root).as_posix()} is a symlink. "
                "A bundle carries files, never links to files the far side owns."
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError(
                f"The bundle member {path.relative_to(root).as_posix()} is not a regular file."
            )
        relative = path.relative_to(root).as_posix()
        if relative == BUNDLE_FILENAME:
            continue
        yield relative, path


def verify_bundle_files(directory: str | Path, manifest: BundleManifest) -> None:
    """Every listed file is present and hashes as claimed, and nothing else is.

    The second half is the one that matters. A ``files[]`` covering three of
    four members leaves the fourth unvouched-for inside a bundle whose every
    listed hash checks out — so the directory is walked and an unlisted member
    is refused exactly as loudly as a modified one.
    """
    try:
        root = trusted_directory(directory, label="bundle directory")
    except TemplateSecurityError as exc:
        raise BundleError(str(exc)) from exc

    on_disk = dict(_walk(root))
    listed = {entry.path: entry.sha256 for entry in manifest.files}

    unlisted = sorted(set(on_disk) - set(listed))
    if unlisted:
        raise BundleError(
            f"The bundle carries {', '.join(unlisted)}, which {BUNDLE_FILENAME} does not "
            "list. Every member is pinned by hash or the bundle is refused."
        )
    missing = sorted(set(listed) - set(on_disk))
    if missing:
        raise BundleError(
            f"{BUNDLE_FILENAME} lists {', '.join(missing)}, which the bundle does not carry."
        )
    for relative, path in sorted(on_disk.items()):
        actual = file_digest(path)
        if actual != listed[relative]:
            raise BundleError(
                f"The bundle member {relative} does not match the hash "
                f"{BUNDLE_FILENAME} pins for it. It was modified after it was exported."
            )


def files_for(root: Path, relatives: Iterable[str]) -> tuple[BundleFile, ...]:
    """Build a ``files[]`` list for *relatives* under *root*, in path order."""
    return tuple(
        BundleFile(path=relative, sha256=file_digest(root / relative))
        for relative in sorted(relatives)
    )


def describe_requires(requires: Requires) -> list[str]:
    """One human line per requirement, for a plan or a CLI print."""
    lines: list[str] = []
    for kind in REQUIRE_KINDS:
        values: tuple[str, ...] = getattr(requires, kind)
        if values:
            lines.append(f"{kind}: {', '.join(values)}")
    return lines
