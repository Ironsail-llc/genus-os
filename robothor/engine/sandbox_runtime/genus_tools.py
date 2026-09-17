"""Call your agent's tools from inside `execute_code`.

    from genus_tools import web_fetch, read_file
    import genus_tools

    for paper_id in ids:                      # one model turn, not fifty
        page = web_fetch(url=f"https://example.org/{paper_id}")
        ...
    genus_tools.call("list_people", query="acme")

Every function here is the tool of the same name. Import any tool your agent is
allowed — the module resolves names on demand — or use `call(name, **args)`
when the name is computed. `tools()` lists what this run may reach.

A call returns the tool's own result dictionary, errors included: a tool that
failed comes back as `{"error": "..."}` rather than raising, so a loop over
fifty items is not ended by item seventeen. `ToolError` is raised only when the
CALL could not be made at all — the per-snippet limit is spent, the tool may
not be reached from code, or the channel to the engine is gone. Those end the
snippet on purpose: continuing past them would spin.

Nothing here holds a credential or touches a database. It is a socket, and on
the other side of it every call goes through the same permission, guardrail and
audit path a tool call from a turn does. A tool your agent could not call from
a turn is refused here too.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

__all__ = ["ToolError", "call", "tools"]

_DIR = os.environ.get("GENUS_TOOLS_DIR", "")
_RECV = 65536


class ToolError(RuntimeError):
    """The call could not be made. Not the same as a tool that failed."""


def _endpoint() -> tuple[str, str]:
    if not _DIR:
        raise ToolError(
            "genus_tools is only available inside execute_code (GENUS_TOOLS_DIR is unset)"
        )
    root = Path(_DIR)
    try:
        token = (root / "token").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ToolError(f"genus_tools could not read its token: {exc}") from exc
    return str(root / "rpc.sock"), token


def _request(payload: dict[str, Any]) -> dict[str, Any]:
    """One round trip. A fresh connection per call, so nothing is shared."""
    socket_path, token = _endpoint()
    payload = dict(payload, token=token)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)
            sock.sendall(json.dumps(payload, default=str).encode("utf-8") + b"\n")
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(_RECV)
                if not chunk:
                    break
                buffer += chunk
    except OSError as exc:
        raise ToolError(f"genus_tools lost its connection to the engine: {exc}") from exc
    if not buffer.strip():
        raise ToolError("genus_tools got an empty answer from the engine")
    try:
        reply = json.loads(buffer.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("genus_tools could not read the engine's answer") from exc
    if not isinstance(reply, dict):
        raise ToolError("genus_tools got an answer it could not read")
    return reply


def call(name: str, **args: Any) -> Any:
    """Call one tool by name. Returns the tool's own result."""
    reply = _request({"op": "call", "name": name, "args": args})
    if reply.get("ok"):
        return reply.get("result")
    raise ToolError(str(reply.get("error") or "the call was refused"))


def tools() -> list[str]:
    """The tool names this snippet may call."""
    reply = _request({"op": "tools"})
    if reply.get("ok"):
        return list(reply.get("result") or [])
    raise ToolError(str(reply.get("error") or "the tool list was refused"))


def __getattr__(name: str) -> Any:
    """`from genus_tools import anything` — the tool of that name.

    Resolved on demand rather than generated up front: the tool set is the
    agent's, it is known only at run time, and a stub for a tool this agent
    does not have would fail later and less clearly than the engine's own
    "not available to this agent".
    """
    if name.startswith("_"):
        raise AttributeError(name)

    def _tool(**args: Any) -> Any:
        return call(name, **args)

    _tool.__name__ = name
    _tool.__qualname__ = name
    _tool.__doc__ = f"Call the `{name}` tool. Keyword arguments are its parameters."
    return _tool
