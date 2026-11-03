"""What a person asked for this turn, for the credential-intake gate.

Intake is a person handing the chat agent a credential. Who is in the
conversation is not enough to decide that: after the agent has read an email,
a web page or any other tool output, an instruction injected there can ask for
a ``vault_set`` or a ``vault_delete`` in the same run. So the vault tools that
change or enumerate the vault are admitted only for what the person TYPED this
turn (``session.typed_message``, set by the channel without a reply quote, an
attachment's text or a forwarded block) asked for:

* ``vault_set`` — the value appears in that text as a whole token (bounded
  by whitespace, quotes, brackets or sentence punctuation, or after ``=`` or
  ``:``), is credential-sized (at least 16 characters, or at least 8 when it
  contains a character that is not a letter or digit), is not made of letters
  alone, and sits beside a name for THIS key: ``NAME=value`` / ``name: value``,
  or in the same clause (no newline or ``.;!?`` between them) within
  :data:`MAX_KEY_DISTANCE` words;
* ``vault_delete`` — that message asks for a removal (delete, remove, revoke,
  forget, drop, clear, rotate out) and names the key;
* ``vault_list`` — that message asks which credentials are stored.

"Names the key" means the vault key itself, a name a reader resolves it by
(``OPENWEATHER_API_KEY``), or its vendor segment (``openweather``).

``vault_get`` and ``vault_test`` never return a value and are not narrowed.
Tool output never reaches ``typed_message``, so nothing a tool returned, and
nothing a file or a quoted message carried in, can satisfy these checks.
"""

from __future__ import annotations

import re
from typing import Any

#: A credential handed over: at least this long, or MIN_SYMBOL_VALUE_CHARS
#: when it carries a character that is not a letter or digit.
MIN_VALUE_CHARS = 16
MIN_SYMBOL_VALUE_CHARS = 8

#: What may sit around a pasted value without being part of it.
_WRAPPING = "\"'`()[]{}<>,;.!?"
_ASSIGNMENT = ("=", ":")

#: How many words may separate a value from the name of the key it is for.
MAX_KEY_DISTANCE = 6

#: Where one clause ends: a newline, or sentence punctuation before a space.
_CLAUSE_END = re.compile(r"\n|[.;!?](?=\s|$)")

_REMOVAL = re.compile(
    r"\b(delete|remove|revoke|forget|drop|clear|rotate[\s-]+out)\b", re.IGNORECASE
)

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


def _is_credential_sized(value: str) -> bool:
    if value.isalpha():
        return False
    if len(value) >= MIN_VALUE_CHARS:
        return True
    return len(value) >= MIN_SYMBOL_VALUE_CHARS and not value.isalnum()


def _is_a_whole_token(message: str, value: str) -> bool:
    """``value`` is one whitespace-separated token of ``message``, give or take
    the quotes and punctuation around it, or the right side of ``NAME=value``."""
    for raw in message.split():
        for token in (raw, raw.strip(_WRAPPING)):
            if token == value:
                return True
            for mark in _ASSIGNMENT:
                if token.endswith(mark + value):
                    return True
    return False


def pasted_by_the_person(message: str, value: str) -> bool:
    """``value`` is a credential-sized whole token the person typed.

    What a refused or failed ``vault_set`` still registers for redaction: the
    person pasted it, so it must not stay in history. An invented value an
    injected call supplies is not in their message, and is not registered,
    which keeps it from crowding real values out of the bounded registry.
    """
    value = (value or "").strip()
    return _is_credential_sized(value) and _is_a_whole_token(message or "", value)


def _token_names_the_key(token: str, key: str) -> bool:
    return _names_the_key(token.strip(_WRAPPING), key)


#: Words that say the person is handing over a credential, not just naming a
#: vendor ("review the github PR").
_STORE_WORD = re.compile(
    r"\b(?:api[\s_-]?keys?|keys?|tokens?|secrets?|credentials?|passwords?)\b", re.IGNORECASE
)

#: A URL, a host path or a filesystem path: a place, never a credential.
_LOCATION = re.compile(r"://|^(?:\.{0,2}/|~/)|^(?:www\.)?[\w-]+(?:\.[\w-]+)+/")


def _means_a_store(clause: str, tokens: list[str], values: list[int], value: str) -> bool:
    """The clause says "this is a credential": a store word outside the value,
    or the value right after ``name:`` / ``name=``."""
    if _STORE_WORD.search(clause.replace(value, " ")):
        return True
    return any(v > 0 and tokens[v - 1].endswith((":", "=")) for v in values)


def _beside_its_key(message: str, key: str, value: str) -> bool:
    """``value`` and a name for ``key`` share a clause, a few words apart.

    The naming token is never the value's own token, so a URL that contains
    the vendor does not name itself, and the clause must say the person is
    handing a credential over.
    """
    if _LOCATION.search(value):
        return False
    for clause in _CLAUSE_END.split(message):
        tokens = clause.split()
        values: list[int] = []
        for index, raw in enumerate(tokens):
            for token in (raw, raw.strip(_WRAPPING)):
                if token == value:
                    values.append(index)
                for mark in _ASSIGNMENT:
                    name, sep, rest = token.rpartition(mark)
                    if not sep or rest != value:
                        continue
                    # A link's query string (`…?vendor=VALUE`) is not the
                    # person naming a credential: the whole token is a place.
                    if _LOCATION.search(name) or "?" in name or "&" in name:
                        return False
                    if name and _names_the_key(name, key):
                        return True
                    values.append(index)
        if not values:
            continue
        names = [
            i for i, raw in enumerate(tokens) if i not in values and _token_names_the_key(raw, key)
        ]
        near = any(abs(v - n) <= MAX_KEY_DISTANCE for v in values for n in names)
        if near and _means_a_store(clause, tokens, values, value):
            return True
    return False


def _handed_over(message: str, key: str, value: str) -> bool:
    return (
        _is_credential_sized(value)
        and _is_a_whole_token(message, value)
        and _beside_its_key(message, key, value)
    )


def _asks_for_a_list(message: str) -> bool:
    return bool(_LIST_VERB.search(message) and _LIST_NOUN.search(message))


def request_gate(tool: str, args: dict[str, Any], message: str) -> str | None:
    """The turn-level gate ``tool`` fails for this person's ``message``, or None."""
    message = message or ""
    if tool == "vault_set":
        value = str(args.get("value") or "").strip()
        key = str(args.get("key") or "")
        return None if _handed_over(message, key, value) else "not_handed_over"
    if tool == "vault_delete":
        key = str(args.get("key") or "")
        asked = bool(_REMOVAL.search(message)) and _names_the_key(message, key)
        return None if asked else "not_requested"
    if tool == "vault_list":
        return None if _asks_for_a_list(message) else "not_requested"
    return None


def request_refusal(tool: str, gate: str) -> tuple[str, str]:
    """The error and unlock for a turn-level refusal."""
    if gate == "not_handed_over":
        return (
            f"{tool} is refused: the person's own message this turn does not hand "
            "over this value for this key (it may have come from a tool result).",
            "The owner or an admin pastes the whole credential in chat and says what it "
            "is for (the vendor or the key name), or an operator stores it with "
            "`genus vault set <key>`.",
        )
    return (
        f"{tool} is refused: the person's own message this turn did not ask for it.",
        "The owner or an admin asks for it in chat, naming the credential, or an "
        "operator uses `genus vault` on the box.",
    )
