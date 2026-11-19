"""The chat session a SERVICE caller lands in when it names none.

``chat._effective_session_key`` used to read an empty key as the main
session and then keep a service token's key verbatim, so an automated probe
that omitted the field wrote into the conversation the owner's webchat and
Telegram share. A service with no key now gets a session of its own, stable
per service identity: the token's subject, not its agent binding, because two
services bound to one agent are still two callers.

Probes and test turns should name their own key anyway (``agent:<agent>:<label>``,
as the benchmark harness does); this is the floor for one that does not.
"""

from __future__ import annotations

__all__ = ["service_session_key"]


def service_session_key(main_session_key: str, service_id: str) -> str:
    """``agent:<agent>:service:<service id>`` for the main session's agent.

    ``:`` is the key's delimiter, so a service id cannot add segments to it.
    It is percent-encoded (``%`` first, then ``:``) rather than replaced, so
    the mapping is one-to-one: ``a:b`` and ``a-b`` are two sessions.
    """
    parts = main_session_key.split(":")
    agent_id = parts[1] if len(parts) >= 2 and parts[1] else "main"
    encoded = service_id.replace("%", "%25").replace(":", "%3A")
    return f"agent:{agent_id}:service:{encoded}"
