"""Which files hold credentials, so no tool ever opens them for a model.

Measured on this instance over the 48 hours to 2026-09-14: sub-agents read
``~/.config/robothor/connectors.env``, the workspace ``.env``, ``crm/.env``,
a config backup's ``robothor.env`` and ``/etc/robothor/secrets.enc.json``
through ``read_file``. The output redactor caught the values every time —
those were the operator's daily "credential warnings" — but a model has no
use for the file that holds a secret: credentials reach tools through the
platform (env, vault, SOPS), never through the transcript. Refusing the read
removes the exposure instead of cleaning up after it.

The rules are about NAMES and LOCATIONS, deliberately: a denylist that has
to open the file to decide has already lost. Documentation and examples
(``*.example``, ``*.md``) stay readable even when named like the real thing.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os

#: Exact basenames that always hold credentials.
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".pgpass",
        ".git-credentials",  # WildClawBench leaked_api, 2026-09-15: `cat ~/.git-credentials`
        ".my.cnf",
        ".boto",
        ".s3cfg",
        ".htpasswd",
        ".vault-token",
        # The AES master key for this instance's whole vault
        # (robothor/vault/crypto.py: `<workspace>/.vault-key`). It was missing,
        # and `ROBOTHOR_WORKSPACE` is on the exec allowlist — so an ungranted
        # agent under `enforce` was told where the key was and allowed to print
        # it, which decrypts every row the vault holds. The hyphen is why the
        # `*.key` pattern did not catch it.
        ".vault-key",
        # Not a credential, and still not for an agent: it is the key the
        # credential FINGERPRINTS are computed under, and reading it puts every
        # fingerprint this instance prints back within reach of an offline
        # dictionary comparison (robothor/secrets/fingerprint.py).
        ".fingerprint-salt",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "token.json",
        "client_secret.json",
        "service-account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "kubeconfig",
    }
)

#: Basename patterns (case-insensitive, whole name).
_SECRET_NAME_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.env(\.[a-z0-9_-]+)*",  # .env, .env.local, .env.production
        r"[a-z0-9_.-]*\.env",  # robothor.env, connectors.env
        r"secrets?[a-z0-9_.-]*\.(json|ya?ml|env|toml|ini)",  # secrets.json, secrets-prod.yaml
        r"[a-z0-9_.-]*\.enc\.(json|ya?ml)",  # SOPS files
        r"[a-z0-9_.-]*\.(pem|key|p12|pfx|kdbx|keystore|jks)",
        r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?",
        r"[a-z0-9_.-]*service[-_]account[a-z0-9_.-]*\.json",
    )
)

#: Suffixes that mark a file as documentation of a secret, not the secret.
_DOC_SUFFIXES = (".example", ".sample", ".template", ".md", ".txt", ".rst")

#: Directory names anywhere in the path that hold nothing but credentials.
#: The platform's own runtime directories (/run/robothor, /etc/robothor) are
#: deliberately NOT here: they hold operational state too (alert and SLO
#: JSON, logs), and a replay of a week of real commands showed those reads
#: were the only false refusals. Their credential files are caught by name.
#:
#: ``gcloud`` joined 2026-09-15 with the exec scrub, and ``.config/gh`` is
#: handled by :data:`_SECRET_DIR_PAIRS` below: once ``GH_TOKEN`` stops being
#: inherited, ``gh`` falls back to its own login file, so reading that file
#: became the way to obtain the operator's personal credential instead.
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", "gcloud"})

#: Directory names that are only a secrets directory under a particular parent.
#: ``gh`` alone is far too common a path component to deny outright; ``.config/gh``
#: is unambiguous.
_SECRET_DIR_PAIRS = frozenset({(".config", "gh"), (".config", "gcloud")})

#: Shell builtins that print the environment of the shell running them — which
#: for an ``exec`` command is a child of the engine. Refused only in their
#: printing form: ``set`` and ``declare``/``typeset`` print everything with no
#: arguments or with ``-p``, while ``set -euo pipefail`` and ``declare -i n=3``
#: are ordinary script lines. A denylist that ate ``set -e`` would break every
#: agent script, and a control that breaks scripts gets turned off.
_ENV_PRINTING_BUILTINS = frozenset({"set", "declare", "typeset"})

#: ``/proc/<pid>/environ`` — the way around the exec scrub that involves no file
#: on disk. A process may read another same-uid process's environment, and
#: ``subprocess.run(shell=True)`` makes the ENGINE the parent of every ``exec``
#: shell, so ``$PPID`` is the process holding the decrypted secrets file.
#:
#: This is a denylist and is worth what a denylist is worth: it raises the cost,
#: it does not close the hole. What closes it is ``PR_SET_DUMPABLE`` in
#: :mod:`robothor.engine.process_hardening`; what removes it is the SOPS shrink,
#: after which the engine's environment holds only bootstrap credentials.
#: Matched against a NORMALISED command, never the raw one. The literal form
#: missed ``/proc/<pid>//environ`` — the kernel resolves both to the same file,
#: and a path denylist that matches the string rather than the path means
#: nothing. ``task/<tid>/`` is the per-thread view of the same environment.
_PROCFS_ENVIRON = re.compile(r"/proc/[^/\s]+/(?:task/[^/\s]+/)?(environ|cmdline)\b")

#: Any ``/proc/…`` run in a command, however it is spelled. Extracted, then
#: normalised with ``posixpath.normpath``, which resolves ``..`` and ``.`` and
#: collapses ``//`` exactly as the kernel does.
#:
#: Hand-rolled collapsing was tried twice and was wrong twice. The first closed
#: ``//`` and ``/./`` and skipped ``..`` on the grounds that resolving it needs
#: a working directory — true of a RELATIVE path and irrelevant here, because a
#: ``/proc/…`` path is rooted, so ``..`` means exactly one thing. A probe then
#: read a canary through ``/proc/<pid>/../<pid>/environ`` while every other
#: spelling was refused, and a regex written for that still missed
#: ``/proc/1/task/1/../../environ``.
#:
#: The lesson is the general one: a denylist that matches a STRING rather than a
#: PATH means nothing, because the kernel resolves every spelling to one file.
#: So the matcher resolves them too, with the standard library rather than by
#: hand.
#:
#: The character class stops at shell metacharacters so a normalisation cannot
#: swallow the rest of a command line. ``$`` and ``{}`` stay in, because
#: ``/proc/$PPID/environ`` and ``/proc/${pid}/environ`` are the spellings an
#: agent actually writes.
_PROC_PATH = re.compile(r"/proc/[^\s'\";|&()<>]*")

#: And the backstop, which is where the spelling game stops.
#:
#: Normalisation handles every path a caller writes out. It cannot handle one
#: the SHELL computes — ``/proc/$(pgrep engine)/environ``, a variable holding
#: half the path, a ``cd`` and a relative read — and each round of this review
#: found one more spelling than the last. So the final rule is blunt and
#: complete: a command that mentions ``/proc`` and asks for ``environ`` or
#: ``cmdline`` is refused, whatever lies between them.
#:
#: The cost is a false refusal for a command that merely NAMES the path (an
#: agent writing documentation about procfs). That is a visible, recoverable
#: annoyance; the alternative is another spelling nobody thought of. And this is
#: defence in depth either way — ``PR_SET_DUMPABLE`` is the control that
#: actually closes the read.
_MENTIONS_PROC_ENVIRON = re.compile(r"/proc\b[\s\S]*?\b(?:environ|cmdline)\b")


def _normalise_paths(command: str) -> str:
    """Rewrite every ``/proc/…`` run in *command* to its canonical spelling."""
    import posixpath

    return _PROC_PATH.sub(lambda match: posixpath.normpath(match.group(0)), command)


#: The channel inbox's quarantine directory, as a SHAPE:
#: ``…/inbox/<channel>/<chat id>/<YYYY-MM-DD>/secret/<file>``. A file the
#: operator sent whose ORIGINAL name said "credentials" is written there by
#: ``robothor.engine.attachments``, because sanitising a name for the
#: filesystem destroys what every rule in this module matches on — ``.env``
#: becomes ``env`` and ``.ssh/id_rsa`` becomes ``id_rsa``.
#:
#: Without this, ``read_file`` and ``send_file`` refused the inbox copy (they
#: ask ``attachments.is_inbox_secret``, which anchors on the resolved workspace)
#: and ``exec`` did not, because ``exec_reads_secret`` gates on this function.
#: So ``cat``/``head``/``grep``/``python3 -c`` printed the operator's own
#: credentials file into the tool result, hence into the model's context and
#: ``agent_run_steps``.
#:
#: Matched as a shape rather than against the resolved inbox root, because this
#: function is PURE by contract and is imported BY ``attachments`` — asking it
#: back would be both a filesystem read and a cycle. The shape is specific
#: enough to carry the weight: four segments in a fixed order with a date among
#: them. ``projects/inbox/secret/design.md`` has one segment where this needs
#: three, so the false positive the last review found here cannot recur. The
#: tools keep their stricter root check on top of this.
_INBOX_SECRET = re.compile(r"(?:^|/)inbox/[^/]+/[^/]+/\d{4}-\d{2}-\d{2}/secret/[^/]+$")


def _expanduser(path: str | os.PathLike[str]) -> str:
    """``~`` and ``~user`` expanded, never raising.

    ``Path.expanduser`` raises ``RuntimeError`` for a ``~word`` that names no
    user, and agents write plenty of those (``"~40% faster"``, ``@~1``,
    ``mask = ~flags``). ``os.path.expanduser`` returns such a word unchanged,
    so it stays a literal and every name rule still applies to it.
    """
    import os as _os

    return _os.path.expanduser(str(path))  # noqa: PTH111 - Path.expanduser raises


def is_inbox_secret_path(path: str | os.PathLike[str]) -> bool:
    """True for the channel inbox's copy of a credentials file. Pure.

    Normalised first. This rule matches a directory SEQUENCE, so unlike every
    other rule in this module it cannot fall back on a basename: ``.env`` and
    ``id_rsa`` survive a ``..`` in the middle of a path because ``..`` cannot
    hide the last segment, and this one did not —
    ``…/secret/../secret/<file>`` named the same bytes and walked straight
    past. That is the lesson the vault work wrote into this very module for
    ``/proc``: a denylist that matches a STRING rather than a PATH means
    nothing, because the kernel resolves every spelling to one file.

    ``normpath`` is textual — no filesystem access, no symlink resolution — so
    the function stays pure and still answers the same for a file that does
    not exist. It is also purely narrowing in the direction that matters: a
    ``..`` that walks OUT of the quarantine normalises to a path outside it,
    which is an ordinary file and stays readable.
    """
    import posixpath

    spelled = PurePath(_expanduser(path)).as_posix()
    return bool(_INBOX_SECRET.search(posixpath.normpath(spelled)))


def _in_secret_location(p: PurePath) -> bool:
    """True when a DIRECTORY on the path holds nothing but credentials."""
    parts = [part.lower() for part in p.parts]
    if any(part in _SECRET_DIRS for part in parts):
        return True
    if any(pair in _SECRET_DIR_PAIRS for pair in zip(parts, parts[1:], strict=False)):
        return True
    # A dot-directory the platform reserves for instance secrets: .robothor/secrets*
    return any(
        part == ".robothor" and parts[i + 1].startswith("secret")
        for i, part in enumerate(parts[:-1])
    )


def is_secret_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* names a file that holds credentials.

    Pure: no filesystem access, so the answer is the same for a file that
    does not exist (the refusal must not reveal whether it does).
    """
    p = PurePath(_expanduser(path))
    name = p.name
    lowered = name.lower()

    # BEFORE the documentation suffixes: a file the operator sent as
    # `notes.md` holding their credentials is quarantined on the way in, and
    # `.md` must not read it back out again.
    if is_inbox_secret_path(p) or is_credential_path(p):
        return True

    if lowered.endswith(_DOC_SUFFIXES):
        return False

    if _in_secret_location(p):
        return True

    if lowered in _SECRET_NAMES:
        return True
    return any(pat.fullmatch(name) for pat in _SECRET_NAME_PATTERNS)


#: The two dotenv shapes of :data:`_SECRET_NAME_PATTERNS` (``.env``,
#: ``.env.local``, ``robothor.env``, ``connectors.env``). Kept as their own
#: tuple so :func:`is_maskable_dotenv_path` and the refusal can never disagree
#: about which names are "a dotenv file".
_DOTENV_NAME_PATTERNS = _SECRET_NAME_PATTERNS[:2]

#: The ``secrets*`` pattern: a file NAMED as secrets holds nothing else, so it
#: is refused even when its body is dotenv.
_SECRETS_NAMED = _SECRET_NAME_PATTERNS[2]


def is_maskable_dotenv_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* is a secret path that ``read_file`` may return MASKED.

    The instance env file (``robothor.env``, see
    :data:`robothor.engine.instance_env.DEFAULT_ENV_FILE`) holds the
    operator's settings and their credentials in one place. Refusing it whole
    left the assistant unable to say what its own settings are; masking the
    credential values answers that without putting one in a transcript
    (:mod:`robothor.engine.env_masking`). Only the dotenv NAME shapes qualify,
    and never one under a secrets directory, quarantined in the inbox, or
    named ``secrets*`` -- those stay refused, as do SOPS files and key
    files, which are not dotenv names at all. Pure, like
    :func:`is_secret_path`: the body is checked by the masker after reading.
    """
    p = PurePath(_expanduser(path))
    name = p.name
    lowered = name.lower()
    if not is_secret_path(p) or is_inbox_secret_path(p) or _in_secret_location(p):
        return False
    if is_credential_path(p):  # `.env`, `.env.*`: refused outright, never masked
        return False
    if lowered in _SECRET_NAMES and lowered != ".env":
        return False
    if _SECRETS_NAMED.fullmatch(name):
        return False
    return any(pat.fullmatch(name) for pat in _DOTENV_NAME_PATTERNS)


REFUSAL = (
    "refused: {name} is a secrets file. Credentials are supplied to tools by the "
    "platform and are never read from disk by an agent; if a value is missing, "
    "say so to the operator instead of reading the file."
)


def refusal_for(path: str | os.PathLike[str]) -> str:
    """The error text a tool returns for a refused path (names the file, never a value)."""
    return REFUSAL.format(name=PurePath(str(path)).name)


# ── exec ──────────────────────────────────────────────────────────────────────
#
# The same rule for shell commands, with one distinction ``read_file`` does not
# need: a command may USE a secrets file without printing it. ``. secrets.env``
# then ``curl`` is how an agent runs an authenticated call and never sees the
# value; ``cat secrets.env | grep TOKEN`` puts the value in the transcript.
# Measured on this instance over the week to 2026-09-14, the main agent did
# both. Only the printing shape is refused.

#: Commands whose job is to write a file's contents to stdout.
_PRINTERS = frozenset(
    {
        "cat",
        "tac",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "sed",
        "awk",
        "gawk",
        "cut",
        "sort",
        "uniq",
        "strings",
        "xxd",
        "hexdump",
        "od",
        "base64",
        "base32",
        "tee",
        "paste",
        "column",
        "fold",
        "pr",
        "jq",
        "yq",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
    }
)
#: Prefixes that wrap another command; the real command word follows them.
_WRAPPERS = frozenset({"sudo", "nice", "nohup", "time", "timeout", "command", "exec", "busybox"})

_OPERATORS = frozenset(
    {"|", "||", "&&", "&", ";", ";;", "(", ")", "<", ">", ">>", "<<", "2>", "&>"}
)


def _tokens(line: str) -> list[str]:
    """Shell-aware tokens with operators split out, so a quoted ``\\|`` inside
    a grep pattern is data and a bare ``|`` is a pipe."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for line in command.splitlines():
        current: list[str] = []
        for tok in _tokens(line):
            if tok in _OPERATORS or all(ch in "|&;()<>" for ch in tok):
                if current:
                    out.append(current)
                current = []
            else:
                current.append(tok.lstrip("`"))
        if current:
            out.append(current)
    return out


#: Reserved words and grouping a command word can sit behind: the verb of
#: ``{ cd ~; }``, ``if cd ~; then`` or ``builtin cd ~`` is ``cd``.
_SHELL_KEYWORDS = frozenset(
    {"{", "}", "!", "if", "then", "else", "elif", "do", "done", "while", "until", "for"}
    | {"builtin", "eval"}
)


def _command_word(tokens: list[str]) -> tuple[str | None, list[str]]:
    """The verb of a segment and its arguments, skipping shell keywords,
    wrappers and VAR=val."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SHELL_KEYWORDS:
            i += 1
            continue
        if "=" in tok and not tok.startswith("-") and not tok.startswith("/"):
            i += 1  # FOO=bar prefix
            continue
        if tok in _WRAPPERS:
            i += 1
            if tok == "timeout" and i < len(tokens):
                i += 1  # its duration
            continue
        if tok == "env":
            # `env FOO=1 cmd` and `env -u X cmd` wrap a command; bare `env` prints.
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or "=" in tokens[i]):
                if tokens[i] in ("-u", "--unset") and i + 1 < len(tokens):
                    i += 1
                i += 1
            if i >= len(tokens):
                return "env", []
            continue
        return tok.rsplit("/", 1)[-1], tokens[i + 1 :]
    return None, []


#: Verbs that move the shell for every later segment.
_CD_VERBS = frozenset({"cd", "pushd"})

#: Verbs whose ``-C``/``--directory``/``--cwd`` option moves only their own segment.
_DIR_OPTION_VERBS = frozenset({"git", "make", "tar"})


def _dir_target(target: str, base: str | None) -> str | None:
    """The directory a ``cd`` *target* lands in, or None when it cannot be known
    from the text: a variable, a substitution, a glob, ``-`` or ``~user``."""
    import posixpath

    word = target.strip("'\"")
    if not word or word == "-" or word.startswith("+"):
        return None
    if word.startswith("~") and not _is_home_spelling(word):
        return None
    spelled = _home_spelled(word)
    if any(ch in spelled for ch in "$`") or _GLOB_MAGIC.search(spelled):
        return None
    if not posixpath.isabs(spelled):
        if base is None:
            return None
        spelled = posixpath.join(base, spelled)
    return posixpath.normpath(spelled)


def _option_dir(args: list[str], base: str | None) -> tuple[bool, str | None]:
    """``(found, directory)`` for a ``-C dir`` / ``--directory=dir`` / ``--cwd dir``."""
    found, here = False, base
    for i, arg in enumerate(args):
        value: str | None = None
        if arg in ("-C", "--directory", "--cwd"):
            value = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith(("--directory=", "--cwd=")):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-C") and len(arg) > 2:
            value = arg[2:]
        if value is not None:
            found, here = True, _dir_target(value, base)
    return found, here


#: Words that change the shell's directory wherever they sit in a segment.
_DIR_CHANGE_WORDS = frozenset({"cd", "pushd", "popd"})


def _unquoted_words(command: str) -> set[str]:
    """The words of *command* that are not quoted: ``cd`` in ``xargs cd`` is, in
    ``echo 'cd'`` it is not."""
    out: set[str] = set()
    for line in command.splitlines():
        lexer = shlex.shlex(line, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            out.update(t for t in lexer if "'" not in t and '"' not in t)
        except ValueError:
            out.update(line.split())
    return out


def _env_chdir(tokens: list[str]) -> bool:
    """True when an ``env`` in *tokens* carries ``-C dir`` or ``--chdir``."""
    for i, tok in enumerate(tokens):
        if tok != "env":
            continue
        for opt in tokens[i + 1 :]:
            if not (opt.startswith("-") or "=" in opt):
                break
            if opt in ("-C", "--chdir") or opt.startswith(("--chdir=", "-C")):
                return True
    return False


def _segment_dirs(
    segments: list[list[str]], start: str | None, command: str = ""
) -> list[str | None]:
    """The directory each segment runs in, or None where the text cannot say.

    The shell starts in *start*. A ``cd``/``pushd`` to a literal directory moves
    every later segment there; one to anything else (``cd "$D"``, ``cd -``, a
    ``popd``) leaves the rest unknown. So does an unquoted ``cd``/``pushd``/
    ``popd`` anywhere else in a segment (``xargs cd``), an ``eval``, and
    ``env --chdir``/``env -C``. ``-C dir`` on git, make or tar moves only its
    own segment.
    """
    unquoted = _unquoted_words(command) if command else set()
    out: list[str | None] = []
    cur = start
    for tokens in segments:
        word, args = _command_word(tokens)
        unknown = bool(
            ("eval" in tokens and "eval" in unquoted)
            or _env_chdir(tokens)
            or (word not in _DIR_CHANGE_WORDS and _DIR_CHANGE_WORDS & unquoted & set(tokens))
        )
        if unknown:
            cur = None
        here = cur
        if word in _DIR_OPTION_VERBS:
            found, moved = _option_dir(args, cur)
            if found:
                here = moved
        out.append(here)
        if word in _CD_VERBS:
            operands = [a for a in args if a == "-" or not a.startswith("-")]
            if not operands:
                cur = (_home() or None) if word == "cd" else None
            else:
                cur = _dir_target(operands[0], cur)
        elif word == "popd":
            cur = None
        if unknown:
            cur = None
    return out


#: The key-material name pattern (``*.pem``, ``*.key``, ``*.p12`` ...). Unlike an
#: exact credential name, it also matches words that are not files at all: a jq
#: filter (``.key``, ``.data.key``) is a bare word ending in ``.key``.
_KEY_FILE_PATTERN = _SECRET_NAME_PATTERNS[4]


def _absent_bare_key_word(candidate: str, cwd: str | None) -> bool:
    """True for a bare word that is a secret ONLY by the key-file pattern and
    names no file in *cwd*: ``.key`` in ``jq -r .key``.

    A path spelling (a separator, ``~``, ``$HOME``), an exact credential name
    (``.env``, ``credentials``), a secrets directory, any other secret pattern,
    or a file that exists keeps its refusal. So does any word when *cwd* is None:
    the command changed directory to somewhere the text does not say.
    """
    import os as _os

    if "/" in candidate or candidate.startswith(("~", "$HOME", "${HOME}")):
        return False
    lowered = candidate.lower()
    if lowered in _SECRET_NAMES or is_inbox_secret_path(candidate):
        return False
    if not _KEY_FILE_PATTERN.fullmatch(candidate):
        return False
    if any(p.fullmatch(candidate) for p in _SECRET_NAME_PATTERNS if p is not _KEY_FILE_PATTERN):
        return False
    if cwd is None:
        return False
    return not _os.path.lexists(Path(cwd) / candidate)


#: Names a glob is tried against when the shell's directory is unknown: one of
#: each listed shape, so ``*.key``, ``id_*`` or ``*`` could be a key and
#: ``*.txt`` could not.
_SECRET_SAMPLES = (
    _SECRET_NAMES
    | _SECRET_DIRS
    | {".config", "gh", "id_rsa", "id_ed25519", "server.key", "cert.pem", "client.p12"}
    | {".env.local", "robothor.env", "secrets.json", "secrets.yaml", "x.enc.json"}
    | {"service-account.json", "credentials", "credentials.json", "token.json"}
    | {"client_secret.json", ".npmrc", ".pypirc", ".vault-key"}
)


def _glob_could_name_secret(word: str) -> bool:
    """True when a relative glob or brace *word* could match a secret's name in
    a directory the text does not spell. Over the brace cap it could."""
    import fnmatch

    variants = _expand_braces(word)
    if variants is None:
        return True
    for variant in variants:
        for part in variant.split("/"):
            if not part or not _GLOB_MAGIC.search(part):
                continue
            pat = part.lower()
            for name in _SECRET_SAMPLES:
                if name.startswith(".") and not pat.startswith("."):
                    continue  # `*` and `?` never match a leading dot
                if fnmatch.fnmatchcase(name, pat):
                    return True
    return False


def exec_reads_secret(command: str, *, cwd: str | None = None) -> str | None:
    """The reason a shell command is refused, or None when it may run.

    Refused: a printing command (``cat``, ``grep``, ``head``, ``sed``,
    ``base64`` ...) given a secrets file, or a bare ``printenv``/``env``.
    Allowed: sourcing the file, testing for it, listing its directory,
    counting its lines — anything that uses it without printing it. *cwd* is
    where the command runs: a bare key-file-shaped word that names no file
    there (a jq filter such as ``.key``) is not a secrets file; after a ``cd``
    it is looked up where the ``cd`` lands, and refused when that is unknown.
    """
    # Before the per-segment walk: a procfs environment read has no command word
    # of its own to catch. ``tr '\0' '\n' < /proc/self/environ`` is a redirect,
    # ``grep -a x /proc/$PPID/environ`` hides the path in an argument, and a
    # Python one-liner names no printer at all. Matched on the whole command for
    # that reason — see _PROCFS_ENVIRON for what this is and is not worth.
    if _PROCFS_ENVIRON.search(_normalise_paths(command)) or _MENTIONS_PROC_ENVIRON.search(command):
        return (
            "refused: reading /proc/<pid>/environ or /proc/<pid>/cmdline prints another "
            "process's environment, which holds this instance's credentials. A "
            "credential your agent needs is granted by name in its manifest's "
            "`secrets:` list."
        )

    segments = _segments(command)
    dirs = _segment_dirs(segments, cwd or str(Path.cwd()), command)
    for tokens, here in zip(segments, dirs, strict=True):
        word, args = _command_word(tokens)
        if word is None:
            continue
        if (
            word == "printenv"
            or (word == "env" and not args)
            or (word == "export" and args == ["-p"])
            or (word in _ENV_PRINTING_BUILTINS and (not args or args == ["-p"]))
        ):
            return f"refused: `{word}` prints the process environment, which holds credentials"
        if word in _PRINTERS:
            for arg in args:
                if arg.startswith("-"):
                    continue
                candidate = arg.strip("'\"")
                if (
                    here is None
                    and _GLOB_MAGIC.search(candidate)
                    and not candidate.startswith(("/", "~", "$HOME", "${HOME}"))
                    and _glob_could_name_secret(candidate)
                ):
                    return (
                        f"refused: `{word}` would expand {candidate} in a directory the "
                        "command changed to, where it could name a secrets file. Spell the "
                        "directory, or name the files, instead."
                    )
                if is_secret_path(candidate) and not _absent_bare_key_word(candidate, here):
                    return (
                        f"refused: `{word}` would print {PurePath(candidate).name}, a secrets "
                        "file. Credentials are supplied to tools by the platform; source the "
                        "file to use a value, or ask the operator, but never print it."
                    )
    return None


# ── credential_path: never read, never named in a command ─────────────────────
#
# A read-only question once led an owner-driven run to fall back to `exec` on
# the engine host and `cat` a CLI tool's OAuth credentials file under the home
# directory: its client secret and refresh token reached the model's context
# and the stored tool output, and the model then called an API with the token.
# The printing rule above did not apply (the CLI's directory and file names were
# on no list). These paths are stricter than the rest of the module: a command
# that so much as NAMES one is refused, whatever its verb, because a CLI or a
# one-liner can read a file no printer word ever touches. Metadata-only verbs
# (`ls`, `test`, `stat`, `wc`, `du`) are the exception.

#: The gate a refusal names, as ``error_type`` and in its text.
CREDENTIAL_GATE = "credential_path"

#: Directories, anywhere on a path, that hold nothing but credentials.
_CREDENTIAL_DIRS = frozenset({".ssh", ".aws", ".kube", ".docker", ".gnupg", "gcloud"})

#: Directories directly under ``.config`` whose NAME starts with one of these:
#: the Google Cloud and Google Workspace CLIs (``gws``, ``gog`` and their
#: suffixed variants) and ``gh``.
_CONFIG_PREFIXES = ("gcloud", "gws", "gog")
_CONFIG_EXACT = frozenset({"gh"})

#: Exact basenames.
_CREDENTIAL_NAMES = frozenset(
    {".netrc", ".npmrc", ".pypirc", ".vault-key", ".git-credentials", ".pgpass"}
)

#: Whole-basename patterns. ``token*.json`` is spelled so ``tokenizer.json`` (a
#: model file, not a credential) stays readable.
_CREDENTIAL_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.env(\.[a-z0-9_-]+)*",
        r"credentials[a-z0-9_.-]*\.json",
        r"tokens?([-_.][a-z0-9_.-]*)?\.json",
        r"client_secrets?[a-z0-9_.-]*\.json",
    )
)

#: Key material counts only under the home directory: a ``*.pem`` in a test
#: fixture tree elsewhere is not the service user's key.
_HOME_KEY = re.compile(r"[^/]*\.(pem|key)", re.IGNORECASE)

#: Verbs that report on a file without reading what is in it.
_METADATA_VERBS = frozenset({"ls", "test", "[", "[[", "stat", "wc", "du"})

#: Path-shaped runs inside one shell word (``-d @x``, ``--file=x``, a Python
#: string literal in ``python3 -c``).
_PATHISH = re.compile(r"(?:\$\{HOME\}|\$HOME|~)?[\w./~*?\[\]-]+")

CREDENTIAL_REFUSAL = (
    "refused by credential_path: {name} is a secrets file that holds credentials. "
    "The operator hands credentials over through the vault (a `secrets:` grant or "
    "vault_get), never by an agent reading a file; ask the operator for what is "
    "missing instead of reading, printing or passing the file."
)


def _home() -> str:
    try:
        return str(Path.home())
    except RuntimeError:
        return ""


def credential_roots(workspace: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """Where a glob may expand and a symlink may resolve: the home directory and
    the workspace, never the whole filesystem. The workspace is first so a
    relative path token joins onto it."""
    roots: list[str] = []
    if workspace:
        roots.append(_expanduser(workspace))
    if home := _home():
        roots.append(home)
    return tuple(roots)


def _home_spelled(word: str) -> str:
    """*word* with ``$HOME``, ``${HOME}`` and a leading ``~`` expanded. Never raises."""
    home = _home()
    if home:
        word = word.replace("${HOME}", home).replace("$HOME", home)
    return _expanduser(word)


def _is_home_spelling(word: str) -> bool:
    """True for ``~``, ``~/...``, ``~user/...`` of a real user, ``$HOME...``."""
    if word.startswith(("$HOME", "${HOME}")):
        return True
    return word.startswith("~") and _expanduser(word) != word


def is_credential_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* is on the credential_path list. Pure (no filesystem access).

    A ``~word`` that names no user stays a literal: the name rules still apply
    to it, and nothing raises.
    """
    import posixpath

    home = _home()
    spelled = posixpath.normpath(PurePath(_home_spelled(str(path))).as_posix())
    p = PurePath(spelled)
    parts = [part.lower() for part in p.parts]
    if any(part in _CREDENTIAL_DIRS for part in parts):
        return True
    for parent, child in zip(parts, parts[1:], strict=False):
        if parent == ".config" and (child in _CONFIG_EXACT or child.startswith(_CONFIG_PREFIXES)):
            return True
    name = p.name
    if name.lower().endswith(_DOC_SUFFIXES):
        return False
    if name.lower() in _CREDENTIAL_NAMES or any(
        pat.fullmatch(name) for pat in _CREDENTIAL_PATTERNS
    ):
        return True
    return _under(spelled, home) and bool(_HOME_KEY.fullmatch(name))


def _under(path: str, root: str) -> bool:
    """*path* is *root* or inside it. A string prefix is not enough: ``/a/wsx``
    is not inside ``/a/ws``."""
    if not root:
        return False
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root if root == "/" else root + "/")


def credential_refusal(path: str | os.PathLike[str]) -> dict[str, str]:
    """The result a tool returns for a credential path (names the file, never a value)."""
    name = PurePath(str(path)).name or str(path)
    return {"error": CREDENTIAL_REFUSAL.format(name=name), "error_type": CREDENTIAL_GATE}


def _named_credential(text: str) -> str | None:
    for match in _PATHISH.finditer(text):
        if is_credential_path(match.group(0)):
            return match.group(0)
    return None


#: Glob magic in a shell word: ``*``, ``?``, a ``[class]`` or a ``{a,b}`` brace.
_GLOB_MAGIC = re.compile(r"[*?[]|\{[^}]*,[^}]*\}")

#: A word a pipeline feeds into a reader, so a metadata verb naming a listed
#: directory upstream (``ls ~/.ssh | xargs cat``, ``find ~/.aws -exec cat``) is
#: not exempt: the directory listing becomes file contents downstream.
_PIPELINE_READERS = frozenset({"xargs"})

#: Directory entries one glob word may visit before the walk gives up. It bounds
#: the WALK, not the matches: a ``**`` over a large tree must cost a bounded
#: number of ``scandir`` entries, whatever it would have matched.
_GLOB_WALK_CAP = 5000

#: Brace variants one word may expand to. Brace groups multiply, so a word with
#: twenty ``{a,b}`` groups is a million variants; past the cap the word is not
#: expanded and only the literal word check applies to it.
_BRACE_CAP = 64


#: A word made only of filename characters and glob or brace syntax: ``*``,
#: ``l*``, ``{a,link}``, ``*.json``. The shell expands these against the working
#: directory (the workspace), so they are paths. A JSON body, a ``key=value`` or
#: a quoted sentence has characters this does not allow.
_FILENAME_GLOB = re.compile(r"[\w.~$/*?\[\]{},@+-]+")


def _looks_like_path(word: str) -> bool:
    """Only these words are expanded or resolved: a separator, a home or dot
    start, a filename-shaped glob, or a basename on the list. Never a JSON body
    (``[{"id": 1}]``)."""
    if word[:1] in ("[", "{") and '"' in word:
        return False
    if "/" in word or word.startswith((".", "~", "$HOME", "${HOME}")):
        return True
    if _GLOB_MAGIC.search(word) and _FILENAME_GLOB.fullmatch(word):
        return True
    name = word.rsplit("/", 1)[-1]
    return name.lower() in _CREDENTIAL_NAMES or any(
        pat.fullmatch(name) for pat in _CREDENTIAL_PATTERNS
    )


def _expand_braces(word: str) -> list[str] | None:
    """``a{b,c}d`` -> ``[abd, acd]``, or None past :data:`_BRACE_CAP` variants.

    Counted before anything is built, so the cap bounds the work as well as the
    result.
    """
    groups = list(re.finditer(r"\{([^{}]*,[^{}]*)\}", word))
    total = 1
    for group in groups:
        total *= len(group.group(1).split(","))
        if total > _BRACE_CAP:
            return None
    out = [word]
    for group in reversed(groups):
        options = group.group(1).split(",")
        out = [w[: group.start()] + o + w[group.end() :] for w in out for o in options]
    return out


def _realpath_is_credential(spelled: str) -> bool:
    """True when *spelled* resolves (symlinks and all) onto a listed path.

    ``os.path.realpath`` resolves every component, so a symlink to a listed file
    and a file under a symlinked directory both land on it. Never raises.
    """
    import os as _os

    try:
        return is_credential_path(_os.path.realpath(spelled))
    except (OSError, ValueError):
        return False


class _WalkBudget:
    """Directory entries left for one glob word's walk."""

    def __init__(self, cap: int) -> None:
        self.left = cap

    def spend(self) -> bool:
        self.left -= 1
        return self.left >= 0


def _scan(directory: str, budget: _WalkBudget) -> list[tuple[str, bool]]:
    """``(name, is_dir)`` for *directory*'s entries, within *budget*."""
    import os as _os

    out: list[tuple[str, bool]] = []
    try:
        with _os.scandir(directory) as entries:
            for entry in entries:
                if not budget.spend():
                    break
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                out.append((entry.name, is_dir))
    except OSError:
        pass
    return out


def _bounded_glob(pattern: str, budget: _WalkBudget) -> list[str]:
    """Shell-style glob of an absolute *pattern*, visiting at most *budget* entries.

    The shell's rules: ``*`` and ``?`` do not match a leading dot unless the
    component starts with one, and ``**`` is any depth of non-hidden directories.
    """
    import fnmatch
    import posixpath

    parts = [p for p in pattern.split("/") if p]
    current = ["/"]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        nxt: list[str] = []
        if not _GLOB_MAGIC.search(part):
            nxt = [posixpath.join(c, part) for c in current]
        elif part == "**":
            stack = list(current)
            while stack and budget.left > 0:
                d = stack.pop()
                nxt.append(d)
                for name, is_dir in _scan(d, budget):
                    if name.startswith("."):
                        continue
                    if is_dir:
                        stack.append(posixpath.join(d, name))
                    elif last:  # a final ``**`` is ``*`` under sh: files too
                        nxt.append(posixpath.join(d, name))
        else:
            for c in current:
                for name, is_dir in _scan(c, budget):
                    if name.startswith(".") and not part.startswith("."):
                        continue
                    if (last or is_dir) and fnmatch.fnmatchcase(name, part):
                        nxt.append(posixpath.join(c, name))
        current = nxt
        if not current or budget.left <= 0:
            break
    return current


def _glob_hits_credential(word: str, roots: tuple[str, ...], base: str | None = None) -> str | None:
    """A listed path a glob word expands onto, or None.

    Bounded: braces past :data:`_BRACE_CAP` are not expanded, and the walk
    visits at most :data:`_GLOB_WALK_CAP` entries. Confined: only a pattern
    inside *roots* (the workspace and the home directory) is walked, and only a
    hit inside them is considered. A relative word expands in *base*, the
    directory its segment runs in (the workspace unless a ``cd`` moved it).
    """
    import os as _os
    import posixpath

    variants = _expand_braces(word)
    if variants is None:
        return None
    budget = _WalkBudget(_GLOB_WALK_CAP)
    for variant in variants:
        pattern = _home_spelled(variant)
        if variant.startswith("~") and not _is_home_spelling(variant):
            continue  # a `~word` naming no user is a literal, not a home
        if not posixpath.isabs(pattern):
            start = base or (roots[0] if roots else None)
            if start is None:
                continue
            pattern = posixpath.join(start, pattern)
        pattern = posixpath.normpath(pattern)
        if not any(_under(pattern, r) for r in roots):
            continue
        for hit in _bounded_glob(pattern, budget):
            if not any(_under(posixpath.normpath(hit), r) for r in roots):
                continue
            if not _os.path.lexists(hit):
                continue
            if is_credential_path(hit) or _realpath_is_credential(hit):
                return hit
        if budget.left <= 0:
            break
    return None


def _resolvable(token: str, roots: tuple[str, ...], base: str | None = None) -> str | None:
    """*token* as an absolute path to resolve, or None.

    An explicit path (a separator, or a home spelling) is resolved; any other
    word only when it names an entry that exists in *base*, the directory the
    segment runs in (the workspace unless a ``cd`` moved it). So a bare
    ``innocent.txt`` linked onto a key is resolved, and ``.key`` in
    ``jq -r .key`` (no such file) is not.
    """
    import os as _os
    import posixpath

    word = token.strip("'\"")
    if not word:
        return None
    if word.startswith("~") and not _is_home_spelling(word):
        return None
    explicit = "/" in word or _is_home_spelling(word)
    if explicit and not _looks_like_path(word):
        return None
    spelled = _home_spelled(word)
    if not posixpath.isabs(spelled):
        base = base or (roots[0] if roots else None)
        if base is None:
            return None
        spelled = posixpath.join(base, spelled)
    if explicit or _os.path.lexists(spelled):
        return spelled
    return None


def exec_names_credential(command: str, *, roots: tuple[str, ...] = ()) -> dict[str, str] | None:
    """The refusal for a command that reaches a credential path, or None.

    Three ways a command reaches one: it NAMES it; a path-like word is
    a glob or brace expansion that expands onto one, or a path that resolves
    (symlinks and all) onto one; or a metadata verb lists a listed directory
    upstream of a reader (``ls ~/.ssh | xargs cat``). *roots* bounds glob
    expansion to the workspace and the home directory. Blocking (it may read the
    filesystem to expand and resolve): call it off the event loop. Never raises.
    """
    segments = _segments(command)
    feeds_reader = any(_command_word(seg)[0] in _PIPELINE_READERS for seg in segments)
    dirs = _segment_dirs(segments, roots[0] if roots else None, command)
    for tokens, here in zip(segments, dirs, strict=True):
        word, _args = _command_word(tokens)
        # A metadata verb (ls, stat, ...) reports on a file without reading it,
        # so it is exempt -- UNLESS its output feeds a reader in the pipeline.
        skip = word in _METADATA_VERBS and not feeds_reader
        for token in tokens:
            globbed = bool(_GLOB_MAGIC.search(token)) and _looks_like_path(token)
            if skip and not globbed:
                continue
            if (hit := _named_credential(token)) is not None:
                return credential_refusal(hit)
            if globbed:
                if (hit := _glob_hits_credential(token, roots, here)) is not None:
                    return credential_refusal(hit)
            elif (path := _resolvable(token, roots, here)) is not None and _realpath_is_credential(
                path
            ):
                return credential_refusal(token)
    return None


def code_names_credential(code: str) -> dict[str, str] | None:
    """The refusal for an ``execute_code`` snippet whose source names a credential path."""
    hit = _named_credential(code)
    return credential_refusal(hit) if hit is not None else None
