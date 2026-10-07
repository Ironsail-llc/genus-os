"""The pull request's own context, shaped for the review prompt.

prepare fetches it; these pure functions turn GitHub's objects into what the
prompt shows: the description (redacted, capped), linked-issue references,
what other reviewers already said (people only, redacted, capped), and the
head commit's checks. Every free-text field passes through the secret
redactor: the review model may quote it into a public review.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from robothor.pr_review.prompt import CheckRun, DiscussionItem

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "DESCRIPTION_MAX",
    "DISCUSSION_ITEM_MAX",
    "DISCUSSION_MAX_ITEMS",
    "checks",
    "discussion",
    "linked_issues",
    "pr_description",
]

#: Characters of the PR description in the prompt.
DESCRIPTION_MAX = 16_000
#: Characters of one review or comment, and how many of them.
DISCUSSION_ITEM_MAX = 1_500
DISCUSSION_MAX_ITEMS = 40
#: Checks listed (failing ones first).
CHECKS_MAX = 40

_CUT = "\n[description cut for length]"
_ISSUE_REF = re.compile(
    r"(?:https://github\.com/(?P<urepo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<unum>\d+))"
    r"|(?:(?<![\w/#])(?P<repo>[\w.-]+/[\w.-]+)#(?P<num>\d+))"
    r"|(?:(?<![\w/&])#(?P<bare>\d+)\b)"
)


def _redact(text: Any) -> str:
    from robothor.secrets.redaction import redact

    return redact(str(text or ""))


def pr_description(pr: Mapping[str, Any], *, max_chars: int = DESCRIPTION_MAX) -> str:
    """The PR body, redacted, cut at ``max_chars`` with a marker."""
    text = _redact(pr.get("body")).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + _CUT
    return text


def linked_issues(body: str, *, limit: int = 10, own_repo: str = "") -> tuple[str, ...]:
    """Issue references in the body, in order, without duplicates.

    A full URL to an issue or pull request in ``own_repo`` (or any URL when it
    is not given) is shortened to ``#N``.
    """
    out: list[str] = []
    for m in _ISSUE_REF.finditer(body or ""):
        if m.group("unum"):
            repo, num = m.group("urepo"), m.group("unum")
            ref = f"#{num}" if not own_repo or repo.lower() == own_repo.lower() else f"{repo}#{num}"
        elif m.group("num"):
            ref = f"{m.group('repo')}#{m.group('num')}"
        else:
            ref = f"#{m.group('bare')}"
        if ref not in out:
            out.append(ref)
        if len(out) >= limit:
            break
    return tuple(out)


def _is_person(user: Mapping[str, Any] | None, exclude: set[str]) -> bool:
    user = user or {}
    login = str(user.get("login") or "")
    if not login or login.lower() in exclude:
        return False
    return str(user.get("type") or "User") != "Bot" and not login.endswith("[bot]")


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rstrip() + " …"


def discussion(
    reviews: Iterable[Mapping[str, Any]],
    review_comments: Iterable[Mapping[str, Any]],
    issue_comments: Iterable[Mapping[str, Any]],
    *,
    exclude_logins: Sequence[str] = (),
    item_chars: int = DISCUSSION_ITEM_MAX,
    max_items: int = DISCUSSION_MAX_ITEMS,
    before: str = "",
) -> tuple[DiscussionItem, ...]:
    """What people (not bots, not us) already said on the PR, oldest first.

    ``before`` (an ISO timestamp) keeps only what was written before it.
    A review with no body counts only when it approved or requested changes.
    """
    exclude = {login.lower() for login in exclude_logins if login}
    items: list[DiscussionItem] = []
    for r in reviews:
        if not _is_person(r.get("user"), exclude):
            continue
        body, state = _redact(r.get("body")).strip(), str(r.get("state") or "")
        if not body and state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        items.append(
            DiscussionItem(
                author=str(r["user"]["login"]),
                kind="review",
                body=_cap(body, item_chars),
                state=state,
                created_at=str(r.get("submitted_at") or ""),
            )
        )
    for c in review_comments:
        if not _is_person(c.get("user"), exclude):
            continue
        line = c.get("line") or c.get("original_line")
        items.append(
            DiscussionItem(
                author=str(c["user"]["login"]),
                kind="review_comment",
                body=_cap(_redact(c.get("body")).strip(), item_chars),
                path=str(c.get("path") or ""),
                line=int(line) if isinstance(line, int) else None,
                created_at=str(c.get("created_at") or ""),
            )
        )
    for c in issue_comments:
        if not _is_person(c.get("user"), exclude):
            continue
        items.append(
            DiscussionItem(
                author=str(c["user"]["login"]),
                kind="comment",
                body=_cap(_redact(c.get("body")).strip(), item_chars),
                created_at=str(c.get("created_at") or ""),
            )
        )
    if before:
        items = [i for i in items if not i.created_at or i.created_at < before]
    items.sort(key=lambda i: i.created_at)
    return tuple(i for i in items if i.body or i.state)[-max_items:]


def checks(
    check_runs: Mapping[str, Any] | None, statuses: Mapping[str, Any] | None = None
) -> tuple[CheckRun, ...]:
    """The head commit's check runs and commit statuses, failing ones first.

    A run still queued or in progress is ``pending``. Commit statuses come
    newest first per context; only the newest counts.
    """
    out: list[CheckRun] = []
    for run in (check_runs or {}).get("check_runs") or []:
        name = _redact(run.get("name")).strip()[:120]
        if not name:
            continue
        done = str(run.get("status") or "") == "completed"
        out.append(
            CheckRun(name=name, conclusion=str(run.get("conclusion") or "") if done else "pending")
        )
    seen: set[str] = set()
    for status in (statuses or {}).get("statuses") or []:
        name = _redact(status.get("context")).strip()[:120]
        if not name or name in seen:
            continue
        seen.add(name)
        state = str(status.get("state") or "")
        out.append(CheckRun(name=name, conclusion="pending" if state == "pending" else state))
    out.sort(key=lambda c: (not c.failing, c.name))
    return tuple(out[:CHECKS_MAX])
