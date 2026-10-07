"""Gmail search syntax -> :class:`~robothor.workspace.types.MailQuery`.

Agents write Gmail queries (``from:bob is:unread newer_than:2d``) and the tool
schema documents that syntax, so it stays the input language for every
provider. This parser gives a non-Google provider a structure to translate,
and never loses anything on the way:

* ``original`` is the caller's string, byte for byte. The Google adapter sends
  it unchanged -- Google keeps receiving exactly the ``q`` it always did.
* Every token the parser does not understand (an operator outside
  :data:`KNOWN_OPERATORS`, a negated group, an unbalanced bracket) is kept in
  ``raw``. A provider that cannot express ``raw`` refuses the search; it never
  quietly drops a term and returns MORE mail than was asked for.

It never raises: a malformed query is still a query Google can answer.
"""

from __future__ import annotations

import re

from robothor.workspace.types import AllOf, AnyOf, MailQuery, QueryNode, Term

__all__ = ["KNOWN_OPERATORS", "parse_query"]

#: Operators with a structured meaning. Anything else stays ``raw``.
KNOWN_OPERATORS = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "is",
        "in",
        "label",
        "after",
        "before",
        "newer_than",
        "older_than",
        "has",
    }
)

#: A quoted phrase, a bracket, or a run of anything else. An operator's quoted
#: value (``subject:"board pack"``) is one token.
_TOKEN_RE = re.compile(r'-?[A-Za-z_]+:"[^"]*"|"[^"]*"|[(){}]|[^\s(){}"]+|"[^"]*$')

_OPEN = {"(": ")", "{": "}"}


class _Unparseable(Exception):  # noqa: N818 - internal control flow
    pass


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _term(token: str) -> Term | None:
    """A token as a :class:`Term`, or ``None`` when it must stay raw."""
    negated = token.startswith("-") and len(token) > 1
    body = token[1:] if negated else token
    if body.startswith('"'):
        if len(body) < 2 or not body.endswith('"'):
            return None
        return Term("phrase", body[1:-1], negated)
    name, sep, value = body.partition(":")
    if not sep:
        return Term("text", body, negated)
    op = name.lower()
    if op not in KNOWN_OPERATORS or not value or ":" in value:
        return None
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            return None
        value = value[1:-1]
    return Term(op, value, negated)


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.raw: list[str] = []

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def sequence(self, close: str | None) -> list[QueryNode]:
        """Clauses until ``close`` (or the end); ``OR`` binds adjacent items."""
        out: list[QueryNode] = []
        while True:
            token = self.peek()
            if token is None:
                if close is not None:
                    raise _Unparseable
                return out
            if token == close:
                self.take()
                return out
            if token in (")", "}"):
                raise _Unparseable
            item = self.item()
            if item is None:
                continue
            alternatives = [item]
            while self.peek() == "OR":
                self.take()
                nxt = self.item() if self.peek() not in (None, ")", "}") else None
                if nxt is None:
                    raise _Unparseable
                alternatives.append(nxt)
            out.append(alternatives[0] if len(alternatives) == 1 else AnyOf(tuple(alternatives)))

    def item(self) -> QueryNode | None:
        token = self.take()
        if token in _OPEN:
            parts = self.sequence(_OPEN[token])
            if not parts:
                raise _Unparseable
            if token == "{":
                return parts[0] if len(parts) == 1 else AnyOf(tuple(parts))
            return parts[0] if len(parts) == 1 else AllOf(tuple(parts))
        if token == "OR" or token == "-" or token.endswith(":"):
            # A dangling OR, a negated group ``-(a b)`` or a grouped operator
            # value ``subject:(a b)``: nothing structured may stand in for it.
            raise _Unparseable
        term = _term(token)
        if term is None:
            self.raw.append(token)
        return term


def parse_query(text: str) -> MailQuery:
    """Parse a Gmail search string. Never raises; see the module docstring."""
    original = text
    tokens = _tokens(text or "")
    parser = _Parser(tokens)
    try:
        clauses = parser.sequence(None)
    except _Unparseable:
        # Unbalanced or dangling syntax: keep the whole thing raw. Google still
        # gets ``original``; nobody else may guess what it meant.
        return MailQuery(original=original, clauses=(), raw=(text,))
    return MailQuery(original=original, clauses=tuple(clauses), raw=tuple(parser.raw))
