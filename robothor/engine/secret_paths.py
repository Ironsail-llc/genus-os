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
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def is_secret_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* names a file that holds credentials.

    Pure: no filesystem access, so the answer is the same for a file that
    does not exist (the refusal must not reveal whether it does).
    """
    p = PurePath(Path(str(path)).expanduser())
    name = p.name
    lowered = name.lower()

    if lowered.endswith(_DOC_SUFFIXES):
        return False

    parts = [part.lower() for part in p.parts]
    if any(part in _SECRET_DIRS for part in parts):
        return True
    # A dot-directory the platform reserves for instance secrets: .robothor/secrets*
    for i, part in enumerate(parts[:-1]):
        if part == ".robothor" and parts[i + 1].startswith("secret"):
            return True

    if lowered in _SECRET_NAMES:
        return True
    return any(pat.fullmatch(name) for pat in _SECRET_NAME_PATTERNS)


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


def _command_word(tokens: list[str]) -> tuple[str | None, list[str]]:
    """The verb of a segment and its arguments, skipping wrappers and VAR=val."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
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


def exec_reads_secret(command: str) -> str | None:
    """The reason a shell command is refused, or None when it may run.

    Refused: a printing command (``cat``, ``grep``, ``head``, ``sed``,
    ``base64`` ...) given a secrets file, or a bare ``printenv``/``env``.
    Allowed: sourcing the file, testing for it, listing its directory,
    counting its lines — anything that uses it without printing it.
    """
    for tokens in _segments(command):
        word, args = _command_word(tokens)
        if word is None:
            continue
        if (
            word == "printenv"
            or (word == "env" and not args)
            or (word == "export" and args == ["-p"])
        ):
            return f"refused: `{word}` prints the process environment, which holds credentials"
        if word in _PRINTERS:
            for arg in args:
                if arg.startswith("-"):
                    continue
                candidate = arg.strip("'\"")
                if is_secret_path(candidate):
                    return (
                        f"refused: `{word}` would print {PurePath(candidate).name}, a secrets "
                        "file. Credentials are supplied to tools by the platform; source the "
                        "file to use a value, or ask the operator, but never print it."
                    )
    return None
