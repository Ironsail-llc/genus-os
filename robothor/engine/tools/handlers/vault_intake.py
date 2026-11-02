"""What a person asked for this turn, for the credential-intake gate.

Intake is a person handing the chat agent a credential. Who is in the
conversation is not enough to decide that: after the agent has read an email,
a web page or any other tool output, an instruction injected there can ask for
a ``vault_set`` or a ``vault_delete`` in the same run. So the vault tools that
change or enumerate the vault are admitted only for what the person's OWN
message this turn (``session.originating_message``) asked for:

* ``vault_set`` — the value being stored appears verbatim in that message;
* ``vault_delete`` — that message names the key: the vault key itself, a name
  a reader resolves it by (``OPENWEATHER_API_KEY``), or its vendor segment;
* ``vault_list`` — that message asks which credentials are stored.

``vault_get`` and ``vault_test`` never return a value and are not narrowed.
Tool output never reaches ``originating_message``, so nothing a tool returned
can satisfy these checks.
"""

from __future__ import annotations

import re
from typing import Any

_LIST_VERB = re.compile(r"\b(list|show|which|what|enumerate)\b", re.IGNORECASE)
_LIST_NOUN = re.compile(
    r"\b(keys?|credentials?|secrets?|tokens?|vault)\b",
    re.IGNORECASE,
)


def _names_for(key: str) -> tuple[str, ...]:
    """Every way a person might name the vault row at ``key``."""
    from robothor.vault.naming import env_name, env_names_for_vault_key, normalise_key

    stored_at = normalise_key(key)
    names = {stored_at, env_name(stored_at), *env_names_for_vault_key(stored_at)}
    segments = [s for s in stored_at.split("/") if s]
    if len(segments) >= 2 and segments[0] in {"providers", "channels"}:
        names.add(segments[1])
    return tuple(n for n in names if n)


def _names_the_key(message: str, key: str) -> bool:
    lowered = message.lower()
    for name in _names_for(key):
        pattern = rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])"
        if re.search(pattern, lowered):
            return True
    return False


def _asks_for_a_list(message: str) -> bool:
    return bool(_LIST_VERB.search(message) and _LIST_NOUN.search(message))


def request_gate(tool: str, args: dict[str, Any], message: str) -> str | None:
    """The turn-level gate ``tool`` fails for this person's ``message``, or None."""
    message = message or ""
    if tool == "vault_set":
        value = str(args.get("value") or "").strip()
        return None if value and value in message else "not_handed_over"
    if tool == "vault_delete":
        return None if _names_the_key(message, str(args.get("key") or "")) else "not_requested"
    if tool == "vault_list":
        return None if _asks_for_a_list(message) else "not_requested"
    return None


def request_refusal(tool: str, gate: str) -> tuple[str, str]:
    """The error and unlock for a turn-level refusal."""
    if gate == "not_handed_over":
        return (
            f"{tool} is refused: the value is not in the person's own message this "
            "turn, so it did not come from them (it may have come from a tool result).",
            "The owner or an admin pastes the credential to the agent in chat, or an "
            "operator stores it with `genus vault set <key>`.",
        )
    return (
        f"{tool} is refused: the person's own message this turn did not ask for it.",
        "The owner or an admin asks for it in chat, naming the credential, or an "
        "operator uses `genus vault` on the box.",
    )
