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

    spelled = PurePath(Path(str(path)).expanduser()).as_posix()
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


#: The settings that MOVE the vault master key and the fingerprint salt out of
#: the workspace (``robothor/vault/crypto.py``, ``robothor/secrets/
#: fingerprint.py``), with their aliases. The basename rules above
#: (``.vault-key``, ``.fingerprint-salt``) cannot see a file the chart mounts
#: as ``key``, so ``read_file`` and ``exec cat`` would print the master key
#: that decrypts every vault row — the very read ``.vault-key`` was added for.
KEY_FILE_SETTINGS = (
    "GENUS_VAULT_KEY_FILE",
    "ROBOTHOR_VAULT_KEY_FILE",
    "GENUS_FINGERPRINT_SALT_FILE",
    "ROBOTHOR_FINGERPRINT_SALT_FILE",
)


def _resolved(path: str) -> str:
    """``realpath``: symlinks followed, ``..`` resolved, missing files allowed."""
    return str(Path(path).expanduser().resolve(strict=False))


def _broad_directories() -> set[str]:
    """Directories too broad to refuse wholesale for holding a key file."""
    from robothor.settings.env import process_env_get

    broad = {"/", _resolved("~")}
    workspace = (process_env_get("ROBOTHOR_WORKSPACE", "") or "").strip()
    broad.add(_resolved(workspace) if workspace else _resolved("~/robothor"))
    return broad


def _is_moved_key_file(path: str | os.PathLike[str]) -> bool:
    """True for an overriding key/salt file, or anything in its directory.

    Read when the check runs, not at import, and RESOLVED on both sides
    (``realpath``), because a denylist that matches a string rather than a
    path means nothing: a symlink, a ``..`` or the Secret mount's own
    timestamped symlink farm all name the same bytes. Every file in the key's
    directory is refused as well — a dedicated mount holds nothing an agent
    needs — unless that directory is the workspace, home or ``/``, where the
    rule would blind the agent to everything — nor when it is an ancestor of
    either.

    The one exception to this module's "pure" contract: resolving a symlink
    reads the filesystem. It still answers the same whether or not the file
    exists, which is what the purity was for.
    """
    configured = _moved_key_files()
    if not configured:
        return False
    candidate = _resolved(str(path))
    broad = _broad_directories()
    for value in configured:
        target = _resolved(value)
        if candidate == target:
            return True
        directory = str(Path(target).parent)
        # Skipped when the key's directory holds (or is) the workspace or home:
        # `/srv/genus/key` beside `/srv/genus/workspace` must not refuse every
        # workspace file. Then only the file itself is refused.
        too_broad = any(Path(wide).is_relative_to(directory) for wide in broad)
        if not too_broad and candidate != directory and Path(candidate).is_relative_to(directory):
            return True
    return False


def _moved_key_files() -> list[str]:
    """The configured override paths, as written, read at call time."""
    from robothor.settings.env import process_env_get

    return [
        value.strip()
        for value in (process_env_get(name, "") or "" for name in KEY_FILE_SETTINGS)
        if value.strip()
    ]


def _moved_key_basenames() -> set[str]:
    """Basenames of the override files and of what they resolve to."""
    names: set[str] = set()
    for value in _moved_key_files():
        names.add(Path(value).name)
        names.add(Path(_resolved(value)).name)
    names.discard("")
    return names


def is_secret_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* names a file that holds credentials.

    Pure but for one rule: no filesystem access, so the answer is the same for
    a file that does not exist (the refusal must not reveal whether it does).
    The exception is :func:`_is_moved_key_file`, which resolves symlinks to
    find a vault key or fingerprint salt moved by its setting.
    """
    p = PurePath(Path(str(path)).expanduser())
    name = p.name
    lowered = name.lower()

    # First, and before the documentation suffixes: a moved key file may be
    # named anything at all.
    if _is_moved_key_file(path):
        return True

    # BEFORE the documentation suffixes: a file the operator sent as
    # `notes.md` holding their credentials is quarantined on the way in, and
    # `.md` must not read it back out again.
    if is_inbox_secret_path(p):
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
    p = PurePath(Path(str(path)).expanduser())
    name = p.name
    lowered = name.lower()
    if not is_secret_path(p) or is_inbox_secret_path(p) or _in_secret_location(p):
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
        # Readers that are not printers. Each copies, encodes or archives a
        # file's bytes somewhere a printer can then reach them — `dd if=<key>
        # | base64` and `cp <key> ws/k && od ws/k` printed the vault master
        # key past a guard that only knew `cat` and friends.
        "dd",
        "cp",
        "mv",
        "install",
        "ln",
        "rsync",
        "scp",
        "tar",
        "zip",
        "gzip",
        "split",
        "uuencode",
        "openssl",
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


def _changed_directories(segments: list[list[str]]) -> list[str]:
    """Every directory a ``cd`` or ``pushd`` in the command moves into.

    A printer argument is otherwise judged against the ENGINE's working
    directory, not the shell's, so ``cd <mount> && cat key`` named the key with
    a path :func:`is_secret_path` never saw.
    """
    found: list[str] = []
    for tokens in segments:
        word, args = _command_word(tokens)
        if word in ("cd", "pushd"):
            targets = [a for a in args if not a.startswith("-")]
            if targets:
                found.append(targets[0].strip("'\""))
    return found


#: Printers whose positional arguments are all FILES. A searcher's first
#: positional argument is its pattern instead, so the bare-basename rule below
#: must not read ``grep key config.yaml`` as printing a file called ``key``.
_FILE_ONLY_PRINTERS = frozenset(
    {
        "cat",
        "tac",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "strings",
        "xxd",
        "hexdump",
        "od",
        "base64",
        "base32",
    }
)
_SEARCHERS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})


def _names_a_pattern(arg: str) -> bool:
    """Whether ``arg`` supplies a search pattern (or a file of them) itself.

    Every spelling: ``-e x`` / ``-f x``, attached ``-ex`` / ``-fx``, inside a
    short-option cluster (``-rnex``), and ``--regexp[=x]`` / ``--file[=x]``.
    Over-matching only treats one more argument as a file, which fails closed.
    """
    if arg in ("--regexp", "--file") or arg.startswith(("--regexp=", "--file=")):
        return True
    return arg.startswith("-") and not arg.startswith("--") and any(ch in "ef" for ch in arg[1:])


def _search_pattern_index(word: str, args: list[str]) -> int | None:
    """Index in ``args`` of a searcher's positional PATTERN, or None.

    None when the pattern was given by an option (``-e``, ``-f``,
    ``--regexp``, ``--file``, in any spelling: then every positional argument is
    a file), or when ``word`` is not a searcher.
    """
    if word not in _SEARCHERS:
        return None
    if any(_names_a_pattern(a) for a in args):
        return None
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return index
    return None


def _relative_names_moved_key(
    candidate: str, shell_dirs: list[str], moved_names: set[str], *, file_only: bool
) -> bool:
    """A RELATIVE printer argument that may be a moved key or salt.

    Refused when it resolves to one under any directory the command ``cd``s
    into. And — because a script or a subshell can change directory where this
    cannot see it — when its basename is the override file's own, but only
    where the argument is plainly a path: it contains a ``/``, a ``cd`` is
    visible, or the printer takes nothing but files (``cat``, ``head``, ``xxd``
    ...). So a SEARCH PATTERN spelled like the key (``grep key config.yaml``,
    ``rg key src/``) is not refused; the caller skips a searcher's pattern
    argument altogether.
    """
    if not moved_names or candidate.startswith(("/", "~")):
        return False
    looks_like_a_path = "/" in candidate or bool(shell_dirs) or file_only
    if looks_like_a_path and Path(candidate).name in moved_names:
        return True
    return any(is_secret_path(str(Path(directory) / candidate)) for directory in shell_dirs)


#: The key-file settings by name, wherever they appear. The exec environment
#: never carries them (``robothor.engine.exec_env``), and a command that names
#: one — ``od $GENUS_VAULT_KEY_FILE``, ``cat "${ROBOTHOR_VAULT_KEY_FILE}"`` —
#: is asking for the master key by reference, so it is refused outright.
_KEY_FILE_VARIABLE = re.compile(r"\b(?:GENUS|ROBOTHOR)_(?:VAULT_KEY_FILE|FINGERPRINT_SALT_FILE)\b")

#: Interpreters whose one-liners open files by a string literal.
_INTERPRETERS = frozenset({"python", "python3", "perl", "ruby", "node"})
_STRING_LITERAL = re.compile(r"""['"]([^'"\s]+)['"]""")

#: ``$(<file)``: bash's read-a-file-into-a-word, with no command at all.
_READ_SUBSTITUTION = re.compile(r"\$\(\s*<\s*([^)\s]+)\s*\)")

#: Redirections whose operand is READ.
_INPUT_REDIRECTS = frozenset({"<", "<<<", "<&", "<>"})


def _mentions_moved_key(command: str, tokens: list[str], shell_dirs: list[str]) -> bool:
    """Defence in depth for a moved key or salt: refused whatever the command.

    The rules above judge a command by its verb, and every verb nobody listed
    was a way through. So when an override is set, ANY mention of the key file
    — or of its dedicated directory — refuses the command: as written or
    resolved, as a substring of the command line (which covers interpreter
    one-liners and globs), or as a token or ``name=value`` operand that
    resolves into it (which covers ``..`` and symlink spellings).
    """
    configured = _moved_key_files()
    if not configured:
        return False
    broad = _broad_directories()
    needles: set[str] = set()
    for value in configured:
        for spelled in (str(Path(value).expanduser()), _resolved(value)):
            needles.add(spelled)
            directory = str(Path(spelled).parent)
            if not any(Path(wide).is_relative_to(directory) for wide in broad):
                needles.add(directory)
    if any(needle and needle in command for needle in needles):
        return True
    for token in tokens:
        for part in (token, token.partition("=")[2]):
            candidate = part.strip("'\"")
            if not candidate:
                continue
            if candidate.startswith(("/", "~")):
                if _is_moved_key_file(candidate):
                    return True
            elif any(_is_moved_key_file(str(Path(d) / candidate)) for d in shell_dirs):
                return True
    return False


def _read_operands(command: str) -> list[str]:
    """Paths a command READS that are not a printer's argument.

    Input redirect operands (``xxd < <file>``, ``base64 <<< ...``), ``dd``'s
    ``if=``/``of=``, ``$(<file)``, and the string literals of an interpreter
    one-liner (``python -c "open('.env').read()"``).
    """
    found: list[str] = [m.group(1) for m in _READ_SUBSTITUTION.finditer(command)]
    for line in command.splitlines():
        tokens = _tokens(line)
        for index, token in enumerate(tokens):
            if token in _INPUT_REDIRECTS and index + 1 < len(tokens):
                found.append(tokens[index + 1])
            elif token.startswith(("if=", "of=")):
                found.append(token[3:])
    for tokens in _segments(command):
        word, args = _command_word(tokens)
        if word in _INTERPRETERS:
            for arg in args:
                found.extend(literal.lstrip("<>+") for literal in _STRING_LITERAL.findall(arg))
    return [f.strip("'\"") for f in found if f.strip("'\"")]


def exec_reads_secret(command: str) -> str | None:
    """The reason a shell command is refused, or None when it may run.

    Refused: a printing command (``cat``, ``grep``, ``head``, ``sed``,
    ``base64`` ...) given a secrets file, or a bare ``printenv``/``env``.
    Allowed: sourcing the file, testing for it, listing its directory,
    counting its lines — anything that uses it without printing it.
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

    if _KEY_FILE_VARIABLE.search(command):
        return (
            "refused: the command names the vault key or fingerprint-salt setting, "
            "which points at the instance's master key. It is never read by an agent."
        )

    segments = _segments(command)
    shell_dirs = _changed_directories(segments)
    all_tokens = [tok for line in command.splitlines() for tok in _tokens(line)]
    if _mentions_moved_key(command, all_tokens, shell_dirs):
        return (
            "refused: the command names the vault master key or fingerprint salt, or "
            "the directory they are mounted in. Neither is ever read by an agent."
        )
    for operand in _read_operands(command):
        if is_secret_path(operand):
            return (
                f"refused: the command would read {PurePath(operand).name}, a secrets "
                "file. Credentials are supplied to tools by the platform; never read "
                "them from disk."
            )

    moved_names = _moved_key_basenames()
    for tokens in segments:
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
            pattern_at = _search_pattern_index(word, args)
            for index, arg in enumerate(args):
                if arg.startswith("-"):
                    continue
                candidate = arg.strip("'\"")
                moved = index != pattern_at and _relative_names_moved_key(
                    candidate, shell_dirs, moved_names, file_only=word in _FILE_ONLY_PRINTERS
                )
                if is_secret_path(candidate) or moved:
                    return (
                        f"refused: `{word}` would print {PurePath(candidate).name}, a secrets "
                        "file. Credentials are supplied to tools by the platform; source the "
                        "file to use a value, or ask the operator, but never print it."
                    )
    return None
