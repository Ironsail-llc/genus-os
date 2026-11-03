"""What a snippet's proxied call may never reach, however it names the tool."""

from __future__ import annotations

from typing import Any

from robothor.engine.tools.constants import SPAWN_TOOLS
from robothor.engine.wrapped_call import inner_call

__all__ = ["PROXY_DENIED_TOOLS", "proxy_denied"]

#: Refused before admission is even asked, whatever the agent is granted.
#:
#: ``execute_code`` because a snippet that can start a snippet is an
#: unbounded recursion the call cap does not bound (each level gets its own
#: fresh cap). ``spawn_agent``/``spawn_agents`` because a spawn runs a whole
#: child runner INLINE in the parent's task, and doing that from inside a
#: subprocess's RPC handler nests two execution models with two deadlines.
#: ``ask_user`` because it blocks on a person, and a loop that can page the
#: operator two hundred times is a denial of service aimed at a human.
PROXY_DENIED_TOOLS: frozenset[str] = frozenset({"execute_code", "ask_user"}) | SPAWN_TOOLS


def proxy_denied(name: str, args: Any) -> str | None:
    """The denied tool this call would run, or None.

    Judged on the call that runs: ``tool_call(name="spawn_agent", ...)`` is a
    spawn, and the allow-set a snippet gets includes ``tool_call``.
    """
    inner = inner_call(name, args)[0]
    return inner if inner in PROXY_DENIED_TOOLS else None
