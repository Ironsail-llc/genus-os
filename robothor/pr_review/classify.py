"""Pull-request links and re-review requests in chat text. Deterministic, no model.

:func:`classify_rereview` is a keyword pass: ``rereview`` for a clear request
("ptal", "re-review", "fixed"), ``other`` for an acknowledgement ("thanks",
"👍"), and ``ambiguous`` for everything it cannot decide — a negated or hedged
request ("not fixed yet", "will push later") or a plain question. The intake
never calls a model; an ambiguous reply is handed to the reviewer agent to
decide, as a task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "MatchedPr",
    "PrRef",
    "classify_rereview",
    "extract_pr_refs",
    "match_allowed_prs",
    "message_search_text",
]

Intent = Literal["rereview", "other", "ambiguous"]

# Owner/repo names: GitHub allows alphanumerics, '-', '_' and '.'.
_PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?=[/?#>|)\]\s\"'*_~,;]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PrRef:
    owner: str
    repo: str
    number: int

    @property
    def key(self) -> str:
        return f"{self.owner.lower()}/{self.repo.lower()}#{self.number}"


@dataclass(frozen=True)
class MatchedPr:
    """A PR link that belongs to a configured repository, in its configured casing."""

    repo_full: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo_full}/pull/{self.number}"


def extract_pr_refs(text: str) -> list[PrRef]:
    """Every github.com pull-request URL in ``text``, deduplicated, in order."""
    seen: set[str] = set()
    out: list[PrRef] = []
    for m in _PR_URL_RE.finditer(text or ""):
        repo = re.sub(r"\.git$", "", m.group(2), flags=re.IGNORECASE)
        ref = PrRef(owner=m.group(1), repo=repo, number=int(m.group(3)))
        if ref.key not in seen:
            seen.add(ref.key)
            out.append(ref)
    return out


def match_allowed_prs(text: str, repos: Iterable[str]) -> list[MatchedPr]:
    """PR links in ``text`` whose repository is in ``repos`` (case-insensitive)."""
    allowed = {r.strip().lower(): r.strip() for r in repos if r and "/" in r}
    out: list[MatchedPr] = []
    for ref in extract_pr_refs(text):
        configured = allowed.get(f"{ref.owner}/{ref.repo}".lower())
        if configured:
            out.append(MatchedPr(repo_full=configured, number=ref.number))
    return out


def message_search_text(message: Mapping[str, Any]) -> str:
    """Everything in a Google Chat message a link can hide in."""
    parts = [
        str(message.get("text") or ""),
        str(message.get("formattedText") or ""),
        str(message.get("argumentText") or ""),
    ]
    for ann in message.get("annotations") or []:
        uri = ((ann or {}).get("richLinkMetadata") or {}).get("uri")
        if uri:
            parts.append(str(uri))
    for att in message.get("attachment") or []:
        uri = (att or {}).get("downloadUri")
        if uri:
            parts.append(str(uri))
    return "\n".join(parts)


# Strong signals that the author wants another pass.
_POSITIVE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bre[-\s]?review\b",
        r"\bre[-\s]?check\b",
        r"\bptal\b",
        r"\bplease take (?:another|a(?:nother)?) look\b",
        r"\b(?:take|have) another look\b",
        r"\bready for (?:another |re-?)?review\b",
        r"\b(?:updated|fixed|addressed|resolved|pushed|done|changes? (?:made|applied|pushed))\b",
        r"\bcan you (?:check|look) again\b",
        r"\blook again\b",
    )
]

# Phrases that flip or hedge a positive keyword ("not fixed yet", "will fix").
_NEGATION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:\bnot|n't|\bnever|\bno)\b[^.!?\n]{0,25}\b"
        r"(?:re[-\s]?review|updated|fixed|addressed|resolved|pushed|done|ready)\b",
        r"\b(?:not yet|yet to|will|going to|gonna|working on|about to|later|tomorrow|wip|"
        r"hold off|wait)\b",
        r"\?\s*$",
    )
]

# Replies that clearly ask for nothing.
_ACKNOWLEDGEMENT = re.compile(
    r"^\s*(?:thanks?|thank you|thx|ty|ok(?:ay)?|k|cool|nice|great|got it|will do|on it|"
    r"sounds good|lgtm|👍|🙏|✅|👀|\W)*\s*$",
    re.IGNORECASE,
)


def classify_rereview(text: str) -> Intent:
    """``rereview``, ``other`` or ``ambiguous`` for one thread reply."""
    t = (text or "").strip()
    if not t:
        return "other"
    positive = any(p.search(t) for p in _POSITIVE)
    negated = any(n.search(t) for n in _NEGATION)
    if positive and not negated:
        return "rereview"
    if positive and negated:
        return "ambiguous"
    if _ACKNOWLEDGEMENT.match(t):
        return "other"
    return "ambiguous"
