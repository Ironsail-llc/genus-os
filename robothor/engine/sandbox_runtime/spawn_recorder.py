"""Records what a snippet's CHILD PROCESSES did, at the layer its own HTTP is recorded.

Copied into the per-call directory beside ``http_recorder.py`` and installed
by the boot script right after it. Standard library only, no import-time side
effects, and importable by the engine for its constants and its pure
classifier — the engine never calls :func:`install`.

Why it exists: ``http_recorder`` hooks ``http.client``, and the measured run
(2026-09-18, Social task 5) made every one of its eighteen writes with
``subprocess.run(["curl", "-s", "-X", "POST", url, "-H", …, "-d", body],
capture_output=True, text=True)``. Nothing in the interpreter saw a request;
every result said ``http_recorder: absent`` about a snippet that had changed
the world eighteen times. Then the snippet consumed the responses and crashed
before printing them, and the three one-shot replies in those bodies existed
nowhere outside the dead process.

Where it hooks: ``subprocess.Popen.__init__`` — ``run``, ``call``,
``check_output`` and a context-managed ``Popen`` all pass through it — for the
concrete argv and whether stdout was a pipe; ``Popen.communicate`` and
``Popen.wait`` for the exit code and, for a known HTTP CLI whose stdout was
piped, the stdout head AS THE SNIPPET RECEIVED IT; and ``os.system`` for the
string it ran. NOT hooked, deliberately: ``os.exec*`` replaces this
interpreter (the hooks go with it) and ``os.posix_spawn`` / ``os.fork`` bypass
``Popen``; the engine-side descendant census is the witness for those, and a
program it saw that this recorder did not is reported as ``egress_unobserved``
rather than as nothing.

What it records, per spawn: the argv head, the program's basename, the
conservative ``(method, url)`` reading of a ``curl``/``wget``/``http``/``xh``
command line, the exit code, a status only when the CLI's exit code proves one
(``curl -f`` exiting 22 is a 4xx+; it is never invented from a 0), the stdout
head, and whether the command was a shell string. A shell string is split with
``shlex`` for classification only; the string itself is what is recorded.
Bounded in count and in bytes, written atomically to a file the engine reads
once the process has exited, and fail-open at every hook: a recorder
exception is swallowed and the snippet's call proceeds untouched.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "EGRESS_TOOLS",
    "HTTP_CLIS",
    "MAX_ARGV_HEAD",
    "MAX_RECORDED_BODY_CHARS",
    "MAX_RECORDED_SPAWNS",
    "RECORD_FILE",
    "classify",
    "flush",
    "install",
]

#: The file the record is written to, inside the per-call directory.
RECORD_FILE = "spawned.json"
#: How many spawns are kept. A loop past this is recorded as its first 200.
MAX_RECORDED_SPAWNS = 200
#: How much of a piped stdout head is kept; the same bound as the HTTP
#: recorder's body, because it is the same evidence.
MAX_RECORDED_BODY_CHARS = 2000
#: How many argv tokens, and how many characters of each, name a spawn.
MAX_ARGV_HEAD = 8
MAX_ARGV_TOKEN_CHARS = 200

#: Command-line HTTP clients whose argv this module can read.
HTTP_CLIS = frozenset({"curl", "wget", "http", "https", "xh"})

#: Programs that reach the network in ways no recorder in this process can
#: see through. A spawn of one of these is reported by the engine as
#: ``egress_unobserved`` — the honest statement that the run may have changed
#: something nothing witnessed. Interpreters are here because a child
#: ``python3 -c "…"`` runs with none of these hooks.
EGRESS_TOOLS = frozenset(
    {
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nc",
        "ncat",
        "socat",
        "sendmail",
        "mail",
        "mutt",
        "git",
        "openssl",
        "telnet",
        "ftp",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
    }
)

_HTTP_VERBS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})

# ── curl ────────────────────────────────────────────────────────────────
#: Short options that consume the next token (or the rest of a bundle).
_CURL_SHORT_VALUE = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
#: Long options that consume the next token unless written ``--opt=value``.
_CURL_LONG_VALUE = frozenset(
    {
        "--abstract-unix-socket",
        "--alt-svc",
        "--aws-sigv4",
        "--cacert",
        "--capath",
        "--cert",
        "--cert-type",
        "--ciphers",
        "--config",
        "--connect-timeout",
        "--connect-to",
        "--continue-at",
        "--cookie",
        "--cookie-jar",
        "--create-file-mode",
        "--crlfile",
        "--curves",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--delegation",
        "--dns-interface",
        "--dns-ipv4-addr",
        "--dns-ipv6-addr",
        "--dns-servers",
        "--doh-url",
        "--dump-header",
        "--egd-file",
        "--engine",
        "--etag-compare",
        "--etag-save",
        "--expect100-timeout",
        "--form",
        "--form-string",
        "--ftp-account",
        "--ftp-alternative-to-user",
        "--ftp-method",
        "--ftp-port",
        "--ftp-ssl-ccc-mode",
        "--happy-eyeballs-timeout-ms",
        "--header",
        "--hostpubmd5",
        "--hostpubsha256",
        "--hsts",
        "--interface",
        "--json",
        "--keepalive-time",
        "--key",
        "--key-type",
        "--krb",
        "--libcurl",
        "--limit-rate",
        "--local-port",
        "--login-options",
        "--mail-auth",
        "--mail-from",
        "--mail-rcpt",
        "--max-filesize",
        "--max-redirs",
        "--max-time",
        "--netrc-file",
        "--noproxy",
        "--oauth2-bearer",
        "--output",
        "--output-dir",
        "--parallel-max",
        "--pass",
        "--pinnedpubkey",
        "--preproxy",
        "--proto",
        "--proto-default",
        "--proto-redir",
        "--proxy",
        "--proxy-cacert",
        "--proxy-capath",
        "--proxy-cert",
        "--proxy-cert-type",
        "--proxy-ciphers",
        "--proxy-crlfile",
        "--proxy-header",
        "--proxy-key",
        "--proxy-key-type",
        "--proxy-pass",
        "--proxy-pinnedpubkey",
        "--proxy-service-name",
        "--proxy-tls13-ciphers",
        "--proxy-tlsauthtype",
        "--proxy-tlspassword",
        "--proxy-tlsuser",
        "--proxy-user",
        "--proxy1.0",
        "--pubkey",
        "--quote",
        "--random-file",
        "--range",
        "--rate",
        "--referer",
        "--request",
        "--request-target",
        "--resolve",
        "--retry",
        "--retry-delay",
        "--retry-max-time",
        "--sasl-authzid",
        "--service-name",
        "--socks4",
        "--socks4a",
        "--socks5",
        "--socks5-gssapi-service",
        "--socks5-hostname",
        "--speed-limit",
        "--speed-time",
        "--stderr",
        "--telnet-option",
        "--tftp-blksize",
        "--time-cond",
        "--tls-max",
        "--tls13-ciphers",
        "--tlsauthtype",
        "--tlspassword",
        "--tlsuser",
        "--trace",
        "--trace-ascii",
        "--trace-config",
        "--unix-socket",
        "--upload-file",
        "--url",
        "--url-query",
        "--user",
        "--user-agent",
        "--variable",
        "--write-out",
    }
)
_CURL_BODY_LONG = frozenset(
    {
        "--data",
        "--data-ascii",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--json",
        "--form",
        "--form-string",
    }
)

# ── wget ────────────────────────────────────────────────────────────────
_WGET_SHORT_VALUE = frozenset("OoaiBtTwQPUeAR")
_WGET_LONG_VALUE = frozenset(
    {
        "--accept",
        "--append-output",
        "--ask-password",
        "--base",
        "--bind-address",
        "--body-data",
        "--body-file",
        "--ca-certificate",
        "--ca-directory",
        "--certificate",
        "--certificate-type",
        "--connect-timeout",
        "--cut-dirs",
        "--directory-prefix",
        "--dns-timeout",
        "--domains",
        "--exclude-directories",
        "--exclude-domains",
        "--execute",
        "--header",
        "--http-password",
        "--http-user",
        "--include-directories",
        "--input-file",
        "--level",
        "--limit-rate",
        "--load-cookies",
        "--local-encoding",
        "--method",
        "--output-document",
        "--output-file",
        "--password",
        "--post-data",
        "--post-file",
        "--prefer-family",
        "--private-key",
        "--private-key-type",
        "--progress",
        "--proxy-password",
        "--proxy-user",
        "--quota",
        "--read-timeout",
        "--referer",
        "--reject",
        "--remote-encoding",
        "--restrict-file-names",
        "--save-cookies",
        "--secure-protocol",
        "--timeout",
        "--tries",
        "--user",
        "--user-agent",
        "--wait",
        "--waitretry",
        "--warc-file",
        "--warc-header",
        "--warc-max-size",
        "--warc-tempdir",
    }
)
_WGET_BODY_LONG = frozenset({"--post-data", "--post-file", "--body-data", "--body-file"})

# ── httpie / xh ─────────────────────────────────────────────────────────
_HTTPIE_SHORT_VALUE = frozenset("aAops")
_HTTPIE_LONG_VALUE = frozenset(
    {
        "--auth",
        "--auth-type",
        "--bearer",
        "--cert",
        "--cert-key",
        "--ciphers",
        "--default-scheme",
        "--format-options",
        "--history-print",
        "--max-headers",
        "--max-redirects",
        "--output",
        "--pretty",
        "--print",
        "--proxy",
        "--raw",
        "--response-charset",
        "--response-mime",
        "--session",
        "--session-read-only",
        "--ssl",
        "--style",
        "--timeout",
        "--unsorted",
        "--verify",
    }
)


def _looks_like_url(token: str) -> str:
    """The token as an absolute URL, or "" when it is not one.

    A scheme is taken as written. ``host[:port]/path`` with no scheme is what
    curl and wget default to ``http://`` — and it is how the mock services
    every graded task talks to are reached.
    """
    low = token.lower()
    if low.startswith(("http://", "https://")):
        return token
    if "://" in token or token.startswith("-") or "/" not in token:
        return ""
    host = token.split("/", 1)[0]
    name, _, port = host.rpartition(":")
    if not name:
        name, port = host, ""
    if port and not port.isdigit():
        return ""
    if not name or not all(c.isalnum() or c in "-._" for c in name):
        return ""
    if not any(c.isalnum() for c in name):
        return ""
    return "http://" + token


def _split_flags(
    args: list[str], short_value: frozenset[str], long_value: frozenset[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """``(flags, positionals, values)`` for a getopt-style command line.

    Bundled short options (``-sfX DELETE``) are unbundled; a value-taking
    option consumes the rest of its bundle or the next token; ``--opt=value``
    is self-contained. ``values`` keeps the LAST value of each value-taking
    option, keyed by its spelling.
    """
    flags: list[str] = []
    positionals: list[str] = []
    values: dict[str, str] = {}
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        i += 1
        if tok == "--":
            positionals.extend(args[i:])
            break
        if tok.startswith("--"):
            name, eq, inline = tok.partition("=")
            flags.append(name)
            if name in long_value:
                if eq:
                    values[name] = inline
                elif i < n:
                    values[name] = args[i]
                    i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            for pos, ch in enumerate(tok[1:], start=1):
                flags.append("-" + ch)
                if ch in short_value:
                    rest = tok[pos + 1 :]
                    if rest:
                        values["-" + ch] = rest
                    elif i < n:
                        values["-" + ch] = args[i]
                        i += 1
                    break
            continue
        positionals.append(tok)
    return flags, positionals, values


def _classify_curl(args: list[str]) -> tuple[str, str]:
    flags, positionals, values = _split_flags(args, _CURL_SHORT_VALUE, _CURL_LONG_VALUE)
    url = _looks_like_url(values.get("--url", ""))
    for tok in positionals:
        url = url or _looks_like_url(tok)
    if not url:
        return "", ""
    explicit = values.get("-X") or values.get("--request")
    if explicit:
        return explicit.upper(), url
    if "-I" in flags or "--head" in flags:
        return "HEAD", url
    if "-G" in flags or "--get" in flags:
        return "GET", url
    if "-d" in flags or "-F" in flags or any(f in _CURL_BODY_LONG for f in flags):
        return "POST", url
    if "-T" in flags or "--upload-file" in flags:
        return "PUT", url
    return "GET", url


def _classify_wget(args: list[str]) -> tuple[str, str]:
    flags, positionals, values = _split_flags(args, _WGET_SHORT_VALUE, _WGET_LONG_VALUE)
    url = ""
    for tok in positionals:
        url = url or _looks_like_url(tok)
    if not url:
        return "", ""
    if values.get("--method"):
        return values["--method"].upper(), url
    if any(f in _WGET_BODY_LONG for f in flags):
        return "POST", url
    return "GET", url


def _classify_httpie(args: list[str]) -> tuple[str, str]:
    _flags, positionals, _values = _split_flags(args, _HTTPIE_SHORT_VALUE, _HTTPIE_LONG_VALUE)
    method = ""
    if positionals and positionals[0].upper() in _HTTP_VERBS:
        method = positionals.pop(0).upper()
    if not positionals:
        return "", ""
    target = positionals.pop(0)
    if target.startswith(":"):
        target = "localhost" + target  # httpie's `:9110/path` shorthand
    url = _looks_like_url(target)
    if not url:
        return "", ""
    if method:
        return method, url
    # A request ITEM that carries data (`a=b`, `a:=1`, `a@file`) makes the
    # default POST; a header (`a:b`) or a query (`a==b`) does not.
    for item in positionals:
        if "==" in item or item.startswith("-"):
            continue
        if "=" in item or "@" in item:
            return "POST", url
    return "GET", url


def _program(token: str) -> str:
    return Path(str(token or "").strip()).name


def _http_command(argv: list[str]) -> tuple[str, list[str]]:
    """``(program, its argv)`` for the first HTTP CLI in *argv*, or ``("", [])``."""
    if not argv:
        return "", []
    if _program(argv[0]) in HTTP_CLIS:
        return _program(argv[0]), list(argv[1:])
    return "", []


def classify(argv: list[str]) -> tuple[str, str]:
    """``(method, url)`` for an HTTP CLI's argv, conservatively; ``("", "")``
    when the command is not one this module reads or its shape is unknown.

    Pure: no state, no filesystem, safe to import engine-side.
    """
    program, rest = _http_command([str(a) for a in argv])
    if program == "curl":
        return _classify_curl(rest)
    if program == "wget":
        return _classify_wget(rest)
    if program in ("http", "https", "xh"):
        return _classify_httpie(rest)
    return "", ""


def _shell_words(command: str) -> list[list[str]]:
    """The simple commands of a shell string, best effort. A string that
    ``shlex`` cannot split (an unbalanced quote) yields nothing."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            segments.append([])
        elif tok.startswith((">", "<", "2>")):
            continue
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _describe(argv: Any, shell: bool) -> dict[str, Any]:
    """The record of one spawn, before its outcome is known."""
    if isinstance(argv, (str, bytes, os.PathLike)):
        text = argv if isinstance(argv, str) else os.fsdecode(argv)
        tokens = [text]
        words = _shell_words(text) if shell else [[text]]
    else:
        tokens = [a if isinstance(a, str) else os.fsdecode(a) for a in list(argv or [])]
        words = [tokens]
    program, method, url, matched = "", "", "", []
    for command in words:
        cli, _rest = _http_command(command)
        if cli:
            program, matched = cli, command
            method, url = classify(command)
            break
    if not program and words and words[0]:
        program = _program(words[0][0])
    rec: dict[str, Any] = {
        "argv_head": [t[:MAX_ARGV_TOKEN_CHARS] for t in tokens[:MAX_ARGV_HEAD]],
        "program": program[:64],
        "method": method,
        "url": url[:2048],
        "returncode": None,
        "status": None,
        "body": b"",
        "shell": bool(shell),
        "http": program in HTTP_CLIS,
        "fail_flag": False,
        "t": time.monotonic(),
    }
    if program == "curl":
        flags, _p, _v = _split_flags(matched[1:], _CURL_SHORT_VALUE, _CURL_LONG_VALUE)
        rec["fail_flag"] = bool({"-f", "--fail", "--fail-with-body"} & set(flags))
    if program in HTTP_CLIS and not url:
        rec["unclassified"] = True
    return rec


_records: list[dict[str, Any]] = []
_path: Path | None = None
_installed = False


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:
            return raw[: exc.start].decode("utf-8", errors="ignore")
        return ""


def _status_for(rec: dict[str, Any]) -> int | None:
    """A status only where the exit code PROVES one. ``curl -f`` exits 22 on
    any 4xx/5xx and ``wget`` exits 8 on a server error response; the exact
    code is unknown, so 400 stands for "refused". Never a 200 from a 0."""
    code = rec.get("returncode")
    if rec.get("program") == "curl" and rec.get("fail_flag") and code == 22:
        return 400
    if rec.get("program") == "wget" and code == 8:
        return 400
    return None


def flush() -> None:
    """Write what has been recorded so far. Atomic, and never raises."""
    if _path is None:
        return
    with contextlib.suppress(Exception):
        out = []
        for rec in _records:
            body = rec["body"]
            text = _text(bytes(body)) if isinstance(body, (bytes, bytearray)) else str(body)
            out.append(
                {
                    "argv_head": rec["argv_head"],
                    "program": rec["program"],
                    "method": rec["method"],
                    "url": rec["url"],
                    "returncode": rec["returncode"],
                    "status": _status_for(rec),
                    "body": text[:MAX_RECORDED_BODY_CHARS],
                    "truncated": len(text) > MAX_RECORDED_BODY_CHARS
                    or len(body) >= MAX_RECORDED_BODY_CHARS * 4,
                    "shell": rec["shell"],
                    "unclassified": bool(rec.get("unclassified")),
                    "t": rec["t"],
                }
            )
        # Per thread, so two children finishing at once cannot tear one tmp file.
        tmp = _path.with_name(f"{_path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        tmp.replace(_path)


def _begin(argv: Any, shell: bool, piped: bool) -> dict[str, Any] | None:
    if len(_records) >= MAX_RECORDED_SPAWNS:
        return None
    rec = _describe(argv, shell)
    rec["piped"] = bool(piped)
    _records.append(rec)
    flush()
    return rec


def _finish(rec: dict[str, Any], returncode: Any, stdout: Any) -> None:
    if isinstance(returncode, int):
        rec["returncode"] = returncode
    if rec.get("http") and rec.get("piped") and stdout is not None:
        if isinstance(stdout, str):
            rec["body"] = stdout[: MAX_RECORDED_BODY_CHARS * 4]
        elif isinstance(stdout, (bytes, bytearray)):
            rec["body"] = bytes(stdout[: MAX_RECORDED_BODY_CHARS * 4])
    rec["t"] = time.monotonic()
    flush()


def _popen_arg(args: tuple, kwargs: dict, name: str, index: int, default: Any) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else default


def install(path: str) -> None:
    """Patch ``subprocess.Popen`` and ``os.system`` once and remember where to
    write. Never raises; a failed hook leaves the original in place."""
    global _installed, _path
    if _installed:
        return
    _path = Path(path)
    popen = subprocess.Popen
    original_init = popen.__init__
    original_communicate = popen.communicate
    original_wait = popen.wait
    original_system = os.system

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        rec = None
        with contextlib.suppress(Exception):
            argv = _popen_arg(args, kwargs, "args", 0, None)
            shell = bool(_popen_arg(args, kwargs, "shell", 8, False))
            stdout = _popen_arg(args, kwargs, "stdout", 4, None)
            rec = _begin(argv, shell, stdout == subprocess.PIPE)
            if rec is not None:
                self._genus_spawn = rec
        original_init(self, *args, **kwargs)

    def communicate(self: Any, *args: Any, **kwargs: Any) -> Any:
        out = original_communicate(self, *args, **kwargs)
        with contextlib.suppress(Exception):
            rec = getattr(self, "_genus_spawn", None)
            if rec is not None:
                stdout = out[0] if isinstance(out, tuple) and out else None
                _finish(rec, self.returncode, stdout)
        return out

    def wait(self: Any, *args: Any, **kwargs: Any) -> Any:
        code = original_wait(self, *args, **kwargs)
        with contextlib.suppress(Exception):
            rec = getattr(self, "_genus_spawn", None)
            if rec is not None and rec.get("returncode") != code:
                _finish(rec, code, None)
        return code

    def system(command: Any) -> Any:
        rec = None
        with contextlib.suppress(Exception):
            rec = _begin(command, True, False)
        status = original_system(command)
        with contextlib.suppress(Exception):
            if rec is not None:
                code = os.waitstatus_to_exitcode(status) if status >= 0 else status
                _finish(rec, code, None)
        return status

    with contextlib.suppress(Exception):
        popen.__init__ = init  # type: ignore[method-assign]
        popen.communicate = communicate  # type: ignore[method-assign]
        popen.wait = wait  # type: ignore[method-assign]
        os.system = system  # type: ignore[assignment]
        atexit.register(_flush_at_exit)
        flush()  # an empty record, so "absent" means the recorder never ran
        _installed = True


def _flush_at_exit() -> None:
    """The exit flush, wrapped once more: an exception raised from an atexit
    callback is printed to the snippet's stderr, which is the one place a
    recorder failure must never show."""
    with contextlib.suppress(Exception):
        flush()
