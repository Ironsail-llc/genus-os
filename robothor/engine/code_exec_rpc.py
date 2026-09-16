"""The socket a sandboxed snippet calls tools over.

One AF_UNIX socket per ``execute_code`` call, in a directory the engine creates
0700 and deletes when the call returns. The protocol is one JSON object per
line in each direction, and it is deliberately tiny — a name, arguments, and a
token — because every field on this wire is a field an untrusted snippet
controls.

What the server is responsible for, and what it is NOT:

* **It authenticates and it bounds.** A wrong token reaches nothing. An
  oversized frame is refused rather than buffered. A call past the cap is
  refused, and the cap is counted HERE as well as in the proxy, so a bug in
  either one still bounds the snippet.
* **It refuses the four tools no snippet may reach**
  (:data:`robothor.engine.tool_proxy.PROXY_DENIED_TOOLS`) before the proxy is
  asked, so recursion and spawning are impossible rather than merely denied.
* **It decides nothing else.** Whether the agent may call ``write_file`` is
  admission's question, and admission is behind the proxy. A second answer
  here would be a second policy, and a second policy drifts.

The token is written to a 0600 file rather than passed in the environment or on
the command line: ``/proc/<pid>/environ`` and ``ps`` are both readable by other
processes of the same uid, and the whole reason the child's environment is
scrubbed is that same-uid is not a boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine.tool_proxy import PROXY_DENIED_TOOLS

if TYPE_CHECKING:
    from robothor.engine.tool_proxy import ToolProxy

logger = logging.getLogger(__name__)

__all__ = ["MAX_REQUEST_BYTES", "SOCKET_NAME", "TOKEN_NAME", "ToolRpcServer"]

#: Names inside the per-call directory. The client derives both from the single
#: ``GENUS_TOOLS_DIR`` it is given, so there is one thing to pass and one thing
#: to get wrong.
SOCKET_NAME = "rpc.sock"
TOKEN_NAME = "token"

#: The largest request frame accepted. A tool call is a name and a few
#: arguments; anything at this size is either a mistake or an attempt to make
#: the engine buffer the snippet's memory on its behalf.
MAX_REQUEST_BYTES = 1_048_576


class ToolRpcServer:
    """Serves one snippet's tool calls for the life of one ``execute_code``."""

    def __init__(
        self,
        *,
        directory: Path | str,
        proxy: ToolProxy,
        max_calls: int,
        max_request_bytes: int = MAX_REQUEST_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self._proxy = proxy
        self.max_calls = max_calls
        self.max_request_bytes = max_request_bytes
        self.token = secrets.token_urlsafe(32)
        self.calls_served = 0
        self._server: asyncio.AbstractServer | None = None

    @property
    def socket_path(self) -> Path:
        return self.directory / SOCKET_NAME

    @property
    def token_path(self) -> Path:
        return self.directory / TOKEN_NAME

    async def start(self) -> None:
        """Create the directory, write the token, and listen."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(stat.S_IRWXU)
        self.token_path.write_text(self.token)
        self.token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.socket_path),
            limit=self.max_request_bytes,
        )
        with contextlib.suppress(OSError):
            self.socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    async def aclose(self) -> None:
        """Stop listening and remove the socket. Never raises."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return
                except asyncio.LimitOverrunError:
                    # The frame is larger than the buffer. Answer and hang up
                    # rather than read the rest: reading it is the attack.
                    await self._reply(
                        writer,
                        {
                            "ok": False,
                            "code": "too_large",
                            "error": (
                                f"request larger than {self.max_request_bytes} bytes — "
                                "write the payload to a file and pass its path"
                            ),
                        },
                    )
                    return
                if not line.strip():
                    return
                reply = await self._respond(line)
                await self._reply(writer, reply)
                if reply.get("code") in ("auth", "too_large"):
                    return
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _respond(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "code": "bad_request", "error": "request was not JSON"}
        if not isinstance(request, dict):
            return {"ok": False, "code": "bad_request", "error": "request was not an object"}

        if not hmac.compare_digest(str(request.get("token", "")), self.token):
            # Loud: the only process that should know this socket exists is the
            # snippet this call started, and it was given the token.
            logger.warning("execute_code RPC: rejected a request with a bad token")
            return {"ok": False, "code": "auth", "error": "not authorised"}

        op = str(request.get("op", "call"))
        if op == "tools":
            return {"ok": True, "result": sorted(self._proxy_allowed() - PROXY_DENIED_TOOLS)}
        if op != "call":
            return {"ok": False, "code": "bad_request", "error": f"unknown op {op!r}"}

        name = str(request.get("name", ""))
        args = request.get("args")
        if not name:
            return {"ok": False, "code": "bad_request", "error": "no tool named"}
        if not isinstance(args, dict):
            args = {}

        if name in PROXY_DENIED_TOOLS:
            return {
                "ok": False,
                "code": "denied",
                "error": (
                    f"'{name}' cannot be called from inside execute_code. "
                    "Call it from a turn instead."
                ),
            }

        if self.calls_served >= self.max_calls:
            return {
                "ok": False,
                "code": "call_cap",
                "error": (
                    f"execute_code tool-call limit reached ({self.max_calls}). "
                    "Narrow the loop, or do the rest in a second snippet."
                ),
            }
        self.calls_served += 1

        try:
            result = await self._proxy.call(name, args)
        except Exception as exc:  # noqa: BLE001 - a broken tool is not a broken socket
            logger.warning("execute_code RPC: %s raised %s", name, type(exc).__name__)
            return {"ok": False, "code": "tool_error", "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "result": result}

    def _proxy_allowed(self) -> frozenset[str]:
        allowed = getattr(self._proxy, "allowed", None)
        return frozenset(allowed or ())

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write(json.dumps(payload, default=str).encode("utf-8") + b"\n")
        with contextlib.suppress(Exception):
            await writer.drain()
