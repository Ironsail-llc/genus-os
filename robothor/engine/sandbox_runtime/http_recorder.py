"""Records the HTTP a snippet makes on its own, so the engine can ask about it.

Copied into the per-call directory beside ``genus_tools.py`` and installed by
the boot script before the snippet runs. Standard library only, no import-time
side effects, and importable by the engine for its three constants — the
engine never calls :func:`install`.

Why it exists: a snippet that sends through ``genus_tools`` leaves every
response on the run's step ledger, so the engine can count the ones the
snippet never printed. A snippet that sends through ``urllib`` — which is how
the measured run sent seventeen messages — leaves the engine nothing but
stdout, and the three follow-ups the service handed back in those responses
were gone before anything could say so.

Where it hooks: ``http.client``, the one layer ``urllib`` and ``requests``
(through ``urllib3``) both go through, and the lowest one that still knows the
method and the URL. ``httpx`` speaks ``h11`` over its own sockets and is not
seen here; a snippet using it is recorded by nothing, which fails in the
direction of silence rather than a false report. A ``curl`` the snippet SPAWNS
never enters this interpreter's HTTP stack at all — that is
``spawn_recorder.py``'s subject, installed right after this one, and the
engine merges the two records into one ``http_calls`` list ordered by the
``t`` each entry carries.

What it records: ``(method, url, status, body head, truncated)`` per
exchange, the body as the snippet consumed it THROUGH THIS LAYER — a body the
snippet never read is a body nobody could have printed, and it is recorded as
empty. So is a body a library read some other way: ``urllib3`` decodes a gzip
or chunked response through its own stream rather than the wrapped ``read``,
so through ``requests`` only an uncompressed ``Content-Length`` reply is seen
(``requests`` asks for gzip by default). ``urllib`` reads every body through
``read()`` and is fully seen. An empty body is "nothing to look for", never
"unread", so the gap costs a missed count, not a false one. Bounded in count
and in bytes, written to a file the engine reads once the process has exited, and
fail-open at every hook: a recorder exception is swallowed — there is no
logger in here that would not write into the snippet's own stderr — and the
snippet's call proceeds untouched.
"""

from __future__ import annotations

import atexit
import contextlib
import http.client
import json
import time
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_RECORDED_BODY_CHARS",
    "MAX_RECORDED_CALLS",
    "RECORD_FILE",
    "flush",
    "install",
]

#: The file the record is written to, inside the per-call directory.
RECORD_FILE = "http_calls.json"
#: How many exchanges are kept. A loop past this is recorded as its first 200.
MAX_RECORDED_CALLS = 200
#: How much of each body. Enough to parse a typical JSON reply whole; a body
#: cut here is still matched by its head.
MAX_RECORDED_BODY_CHARS = 2000

_records: list[dict[str, Any]] = []
_path: Path | None = None
_installed = False


def _is_https(conn: Any) -> bool:
    """urllib3's HTTPSConnection is NOT a subclass of http.client's — an
    `isinstance` test recorded `requests` over HTTPS as `http://host:443/…`,
    a phantom origin no https read could clear. Every TLS connection class
    in the family says so one of three ways: its class, its `scheme`
    attribute (urllib3), or its default port."""
    if isinstance(conn, http.client.HTTPSConnection):
        return True
    if str(getattr(conn, "scheme", "") or "").lower() == "https":
        return True
    return getattr(conn, "default_port", None) == 443


def _absolute(conn: Any, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    scheme = "https" if _is_https(conn) else "http"
    host = getattr(conn, "host", "") or ""
    port = getattr(conn, "port", None)
    default = 443 if scheme == "https" else 80
    netloc = host if not port or port == default else f"{host}:{port}"
    return f"{scheme}://{netloc}{url if url.startswith('/') else '/' + url}"


def _text(raw: bytes) -> str:
    """Bytes the snippet consumed, as text — or "" when they were not text.

    A gzip body is what a compressed API sends and what ``requests`` asks for
    by default; recording its bytes as evidence would flag a response the
    snippet printed in full, because the printed text is not in the stream.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:  # a multibyte character cut by the cap
            return raw[: exc.start].decode("utf-8", errors="ignore")
        return ""


def flush() -> None:
    """Write what has been recorded so far. Atomic, and never raises."""
    if _path is None:
        return
    with contextlib.suppress(Exception):
        out = []
        for rec in _records:
            text = _text(bytes(rec["body"]))
            out.append(
                {
                    "method": rec["method"],
                    "url": rec["url"],
                    "status": rec["status"],
                    "body": text[:MAX_RECORDED_BODY_CHARS],
                    # Said explicitly, because a cut JSON body no longer parses
                    # and the engine must not fall back to matching its raw
                    # head — `print(resp)` shows a repr, which never contains
                    # the raw head, and the reply would be flagged unread
                    # although it was printed in full.
                    "truncated": len(text) > MAX_RECORDED_BODY_CHARS
                    or len(rec["body"]) >= MAX_RECORDED_BODY_CHARS * 4,
                    # When the response arrived, on the same clock the spawn
                    # recorder stamps a finished child with, so the engine can
                    # merge the two records in the order things completed.
                    "t": rec.get("t", 0.0),
                }
            )
        tmp = _path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        tmp.replace(_path)


def _capture(rec: dict[str, Any], chunk: Any) -> None:
    if isinstance(chunk, (bytes, bytearray)) and len(rec["body"]) < MAX_RECORDED_BODY_CHARS * 4:
        rec["body"] += bytes(chunk)


def _attach(response: Any, rec: dict[str, Any]) -> None:
    """Wrap this one response's readers so the body it yields is kept."""
    rec["status"] = int(getattr(response, "status", 0) or 0)

    def wrap_reader(name: str) -> None:
        original = getattr(response, name, None)
        if original is None:
            return

        def reader(*args: Any, **kwargs: Any) -> Any:
            out = original(*args, **kwargs)
            with contextlib.suppress(Exception):
                _capture(rec, out)
            return out

        setattr(response, name, reader)

    for name in ("read", "read1", "readline"):
        wrap_reader(name)

    original_readinto = getattr(response, "readinto", None)
    if original_readinto is not None:

        def readinto(buffer: Any) -> Any:
            n = original_readinto(buffer)
            with contextlib.suppress(Exception):
                if n:
                    _capture(rec, bytes(memoryview(buffer)[:n]))
            return n

        response.readinto = readinto

    original_close = getattr(response, "close", None)
    if original_close is not None:

        def close(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_close(*args, **kwargs)
            finally:
                flush()

        response.close = close


def install(path: str) -> None:
    """Patch ``http.client`` once and remember where to write. Never raises."""
    global _installed, _path
    if _installed:
        return
    _path = Path(path)
    conn_cls = http.client.HTTPConnection
    original_putrequest = conn_cls.putrequest
    original_getresponse = conn_cls.getresponse

    def putrequest(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        with contextlib.suppress(Exception):
            self._genus_pending = (str(method).upper(), _absolute(self, str(url)))
        return original_putrequest(self, method, url, *args, **kwargs)

    def getresponse(self: Any) -> Any:
        response = original_getresponse(self)
        with contextlib.suppress(Exception):
            pending = getattr(self, "_genus_pending", None)
            if pending is not None and len(_records) < MAX_RECORDED_CALLS:
                rec = {
                    "method": pending[0],
                    "url": pending[1],
                    "status": 0,
                    "body": b"",
                    "t": time.monotonic(),
                }
                _records.append(rec)
                _attach(response, rec)
                flush()
            self._genus_pending = None
        return response

    conn_cls.putrequest = putrequest  # type: ignore[method-assign]
    conn_cls.getresponse = getresponse  # type: ignore[method-assign]
    atexit.register(flush)
    _installed = True
