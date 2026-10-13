"""Pull-request links and re-review requests in chat text. Deterministic, no model.

:func:`classify_rereview` is a keyword pass: ``rereview`` for a clear request
("ptal", "re-review", "fixed", "can you re-review?"), ``other`` for an
acknowledgement ("thanks", "👍") or a reviewer's verdict reported in the
thread ("#123: Approved", "changes requested") — including another review
bot's status lines — and ``ambiguous`` for everything it cannot decide: a
negated or hedged request ("not fixed yet", "will push later") or a plain
question. A message of nothing but pull-request links (or ``#123``) is a
request: reposting the links is how a team asks for another look. The intake
never calls a model; it asks the author of an ambiguous reply, in the thread,
to say "re-review".
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
    "mentioned_numbers",
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


# An explicit request for another pass. A question mark does not hedge these:
# "Updated the PR. Can you re-review?" is a request, not a doubt.
_REQUEST = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bre[-\s]?review\b",
        r"\bre[-\s]?check\b",
        r"\bptal\b",
        r"\bplease take (?:another|a(?:nother)?) look\b",
        r"\b(?:take|have) another look\b",
        r"\bready for (?:another |re-?)?review\b",
        r"\bcan you (?:check|look) again\b",
        r"\blook again\b",
    )
]

# A status that implies a request ("fixed", "pushed"). As a question ("is this
# fixed?") it is a doubt, so a question mark makes it ambiguous.
_STATUS = re.compile(
    r"\b(?:updated|fixed|addressed|resolved|pushed|done|changes? (?:made|applied|pushed))\b",
    re.IGNORECASE,
)

# Phrases that flip or hedge a positive keyword ("not fixed yet", "will fix").
_NEGATION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:\bnot|n't|\bnever|\bno)\b[^.!?\n]{0,25}\b"
        r"(?:re[-\s]?review|updated|fixed|addressed|resolved|pushed|done|ready)\b",
        r"\b(?:not yet|yet to|will|going to|gonna|working on|about to|later|tomorrow|wip|"
        r"hold off|wait)\b",
    )
]
_QUESTION = re.compile(r"\?\s*$")

# References to a pull request, stripped before a reply is read: Chat's
# ``<url|label>`` form of a pull-request link, a bare pull-request link,
# ``owner/repo#123``, ``#123`` and a bare number. Nothing else is stripped —
# another URL, an emoji or a "?" is part of what the reply says.
_PR_LINK = r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/pull/\d+"
_PR_REFS = [
    re.compile(rf"<{_PR_LINK}[^>]*>", re.IGNORECASE),
    re.compile(rf"{_PR_LINK}[^\s>|]*", re.IGNORECASE),
    re.compile(r"(?:[\w.-]+/[\w.-]+)?#\d+\b"),
]
_BARE_NUMBER = re.compile(r"(?<![\w/.-])\d+\b")
# What may sit between references in a links-only message.
_SEPARATORS = re.compile(r"[\s,;]+|\band\b", re.IGNORECASE)
# Left at the front once the reference is gone ("<link|#12>: Approved").
_LEADING = re.compile(r"^[\s:,;\-\u2013\u2014]+")

# Replies that clearly ask for nothing.
_ACKNOWLEDGEMENT = re.compile(
    r"^\s*(?:thanks?|thank you|thx|ty|ok(?:ay)?|k|cool|nice|great|got it|will do|on it|"
    r"sounds good|lgtm|👍|🙏|✅|👀|\W)*\s*$",
    re.IGNORECASE,
)

# A reviewer's verdict reported in the thread — by a person or by another
# review bot posting as one ("#123: Approved", "#123: Comments/change request",
# "#123: No changes?", "Changes requested on both"). It is a report about the
# pull request, never a request for one.
_PR_PREFIX = re.compile(r"^\s*#\d+\s*:\s*")
_STATUS_REPORT = re.compile(
    r"^\s*(?:(?:both|all)\s+)?(?:"
    r"approved\b|lgtm\b|no changes\b|merged\b|closed\b|"
    r"changes? requested\b|requested changes\b|"
    r"comments?\s*(?:/|and|or|&)?\s*changes?\s+requests?\b|"
    r"this (?:pr|pull request) (?:is|was) (?:merged|closed)\b|"
    r"a review is already running\b"
    r")",
    re.IGNORECASE,
)


def classify_rereview(text: str) -> Intent:
    """``rereview``, ``other`` or ``ambiguous`` for one thread reply."""
    t = (text or "").strip()
    if not t:
        return "other"
    body, named = _strip_refs(t)
    if named and not _SEPARATORS.sub("", body):
        return "rereview"  # nothing but the pull requests: asking again
    body = _LEADING.sub("", _PR_PREFIX.sub("", body))
    if _STATUS_REPORT.match(body):
        return "other"
    request = any(p.search(body) for p in _REQUEST)
    status = bool(_STATUS.search(body))
    hedged = any(n.search(body) for n in _NEGATION)
    question = bool(_QUESTION.search(body))
    if request and not hedged:
        return "rereview"
    if status and not hedged and not question:
        return "rereview"
    if request or status:
        return "ambiguous"
    if _ACKNOWLEDGEMENT.match(body):
        return "other"
    return "ambiguous"


def _strip_refs(text: str) -> tuple[str, int]:
    """``text`` without its pull-request references, and how many it named."""
    rest, named = text, 0
    for part in _PR_REFS:
        rest, found = part.subn(" ", rest)
        named += found
    return _BARE_NUMBER.sub(" ", rest).strip(), named


_NUMBER_RE = re.compile(r"(?<![\w/.-])#?(\d{1,7})\b")


def mentioned_numbers(text: str) -> set[int]:
    """Pull-request numbers a reply names: links, ``#123`` or a bare ``123``."""
    found = {ref.number for ref in extract_pr_refs(text)}
    found.update(int(m.group(1)) for m in _NUMBER_RE.finditer(text or ""))
    return found
