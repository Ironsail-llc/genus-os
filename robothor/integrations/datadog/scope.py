"""The tenant wall: what each tenant may read from the shared Datadog account.

Several tenants share one Datadog account and one server-held read-only key.
Nothing on Datadog's side keeps them apart. This module does: every query sent
is ``(<tenant scope>) AND (<caller query>)``, and :func:`compose` refuses any
caller query that could change what that wrapping means.

A caller query is refused when, under ANY plausible reading of the search
syntax, it could end the wrapping parenthesis early or leave something open
that swallows it. In practice that means:

* parentheses ``()``, square brackets ``[]`` and braces ``{}`` must nest and
  close in order (a range's brackets count; brackets inside a double-quoted
  phrase are literal and do not);
* double-quoted phrases must close;
* a backslash may not precede a bracket, a quote or another backslash outside
  a phrase, and may not appear inside one at all -- whether the parser honours
  ``\\"`` inside a phrase or ``\\)`` outside it changes where the phrase or
  the group ends, so the query is refused instead of guessed at;
* a single quote outside a double-quoted phrase must pair up, so a parser that
  reads ``'...'`` as a phrase sees the same structure (put text with an
  apostrophe in double quotes);
* no control, format or line-separator characters;
* no more than :data:`MAX_QUERY_CHARS` characters.

Refusing a rare legitimate query is acceptable; letting an escape through is
not. The operator's own scope filters are held to the same rules.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_QUERY_CHARS",
    "NO_ACCESS_QUERY",
    "NO_ACCESS_TAG",
    "DatadogScope",
    "ScopeError",
    "compose",
    "scope_for",
]

#: A malformed-input guard, not a usage limit: no hand-written or
#: agent-written search query comes near this, and refusing a megabyte of
#: brackets keeps the scan and the logged reason bounded.
MAX_QUERY_CHARS = 4000

#: A tag no Datadog metric carries: ANDed into a metric query it matches
#: nothing, so "no access" fails closed even for a caller that does not check.
NO_ACCESS_TAG = "genus_scope:no_access"
#: The monitor (and missing-signal) filter for a tenant granted no access.
#: :func:`compose` refuses it outright; sent raw it would match nothing.
NO_ACCESS_QUERY = f'tag:"{NO_ACCESS_TAG}"'

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = frozenset(_OPEN.values())
#: Characters a backslash outside a phrase may not escape: escaping any of
#: them would make the structure depend on how the parser reads backslashes.
_STRUCTURAL = frozenset("()[]{}\"'\\")
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
#: A metric tag: starts with a letter; no space, comma, brace or quote, any
#: of which could change a ``{tag,tag}`` metric filter.
_TAG = re.compile(r"[A-Za-z][A-Za-z0-9_:./\-]*")


@dataclass(frozen=True)
class DatadogScope:
    """The filters that bound one tenant's reads. ``"*"`` means everything."""

    tenant_id: str
    logs: str
    spans: str
    monitors: str
    metric_tags: tuple[str, ...]


class ScopeError(ValueError):
    """A query was refused before anything was sent. ``reason`` is one plain sentence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _scan(query: str, quotes: str) -> str | None:
    """Why ``query`` is unsafe to wrap when ``quotes`` delimit phrases, or None."""
    stack: list[str] = []
    phrase: str | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if phrase is not None:
            if ch == "\\":
                return "a backslash inside a quoted phrase is not accepted"
            if ch == phrase:
                phrase = None
        elif ch == "\\":
            if i + 1 >= len(query) or query[i + 1] in _STRUCTURAL:
                return "a backslash may not end the query or escape a bracket or quote"
            i += 1
        elif ch in quotes:
            phrase = ch
        elif ch in _OPEN:
            stack.append(_OPEN[ch])
        elif ch in _CLOSE:
            if not stack or stack.pop() != ch:
                return "the brackets or parentheses close out of order"
        i += 1
    if phrase is not None:
        return "a quoted phrase is not closed (put an apostrophe inside double quotes)"
    if stack:
        return "a bracket or parenthesis is not closed"
    return None


def _refusal(query: str) -> str | None:
    """Why ``query`` may not be wrapped, or None when it may."""
    if len(query) > MAX_QUERY_CHARS:
        return f"the query is longer than {MAX_QUERY_CHARS} characters"
    if any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in query):
        return "the query contains a control character or line break"
    # Double quotes are phrases; a parser that also honours single quotes
    # must see the same structure, so the query is scanned both ways.
    return _scan(query, '"') or _scan(query, "\"'")


def compose(scope_query: str, user_query: str) -> str:
    """``(scope) AND (user)``; the scope alone for an empty query; raises :class:`ScopeError`."""
    scope = (scope_query or "").strip()
    user = (user_query or "").strip()
    if not scope or scope == NO_ACCESS_QUERY:
        raise ScopeError("this tenant has no access to this kind of Datadog data")
    if scope != "*" and _refusal(scope) is not None:
        raise ScopeError("this tenant's Datadog scope is malformed")
    reason = _refusal(user)
    if reason is not None:
        raise ScopeError(reason)
    if scope == "*":
        return user or "*"
    if not user:
        return scope
    return f"({scope}) AND ({user})"


# ── the declared scope map ───────────────────────────────────────────────────


def _query(value: Any) -> str | None:
    """A usable scope filter, or None. Blank is NOT everything; only ``*`` is."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    return text if text == "*" or _refusal(text) is None else None


def _entry(tenant_id: str, raw: Any) -> DatadogScope | None:
    if not isinstance(raw, dict):
        return None
    logs = _query(raw.get("logs"))
    if logs is None:
        return None
    spans = _query(raw["spans"]) if "spans" in raw else logs
    monitors = _query(raw["monitors"]) if "monitors" in raw else NO_ACCESS_QUERY
    if spans is None or monitors is None:
        return None
    tags: tuple[str, ...] = (NO_ACCESS_TAG,)
    if "metric_tags" in raw:
        listed = raw["metric_tags"]
        if not isinstance(listed, list) or not all(
            isinstance(t, str) and _TAG.fullmatch(t) for t in listed
        ):
            return None
        tags = tuple(listed)
    return DatadogScope(tenant_id, logs, spans, monitors, tags)


def _declared() -> str:
    from robothor.settings import get_settings

    return get_settings().integrations.datadog_scopes or ""


def scope_for(tenant_id: str) -> DatadogScope | None:
    """``tenant_id``'s scope from ``ROBOTHOR_DATADOG_SCOPES``; None = not configured (refuse)."""
    raw = _declared().strip()
    if not raw:
        return None
    try:
        declared = json.loads(raw)
    except ValueError:
        logger.warning("datadog: ROBOTHOR_DATADOG_SCOPES is not valid JSON; no tenant has a scope")
        return None
    if not isinstance(declared, dict):
        logger.warning(
            "datadog: ROBOTHOR_DATADOG_SCOPES is not a JSON object; no tenant has a scope"
        )
        return None
    if tenant_id not in declared:
        return None
    scope = _entry(tenant_id, declared[tenant_id])
    if scope is None:
        # The tenant id is the operator's own key, not log content; the entry
        # itself is never printed.
        logger.warning(
            "datadog: the scope entry for tenant %s is malformed; it is refused", tenant_id
        )
    return scope
