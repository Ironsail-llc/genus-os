"""How a review is shaped before it is posted to GitHub. Pure functions only.

GitHub rejects the WHOLE review with a 422 when one inline comment points at a
line that is not part of the pull request's diff. A reviewer that cites a line
it read in the full file, rather than in the diff, therefore loses every
finding at once. :func:`partition_comments` keeps an anchor inline only when
the diff can carry it and moves the rest into the review body, where nothing
is lost.

The other rules here are what the review tools agree on:

* :func:`split_by_severity` — only blocking findings are worth an inline
  thread; minor issues and nits go in the body for awareness.
* :func:`compose_body` — summary, previous findings' status, out-of-diff
  blocking findings, then the non-blocking list.
* :func:`own_pr_body` — GitHub refuses APPROVE and REQUEST_CHANGES on a pull
  request the token's own user opened, so the verdict is posted as a COMMENT
  whose first line states it.
* :func:`decide_review` — never two reviews for one head SHA; a re-review is
  incremental from the last reviewed SHA unless history was rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

__all__ = [
    "REVIEW_BODY_MAX_CHARS",
    "DEFAULT_INLINE_SEVERITIES",
    "FULL_REVIEW_COMPARE_STATUSES",
    "ReviewDecision",
    "commentable_lines",
    "compose_body",
    "decide_review",
    "format_issue_comment",
    "own_pr_body",
    "partition_comments",
    "split_by_severity",
]

Side = Literal["LEFT", "RIGHT"]

#: Severities that earn an inline thread. Everything else is body-only.
DEFAULT_INLINE_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})

#: Compare statuses (``base...head``) that make an incremental review
#: meaningless: the last reviewed SHA is no longer an ancestor of the head
#: (force-push, rebase) or no longer exists, so "what changed since" has no
#: answer and the whole pull request must be read again.
FULL_REVIEW_COMPARE_STATUSES: frozenset[str] = frozenset({"diverged", "behind", "missing"})

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(patch: str | None) -> dict[str, dict[int, int]]:
    """Lines of one file's diff that GitHub accepts a review comment on.

    Returns ``{"LEFT": {line: hunk}, "RIGHT": {line: hunk}}``. LEFT numbers are
    the old file's (removed and context lines), RIGHT the new file's (added and
    context lines). The hunk index lets a multi-line range be checked for
    staying inside one hunk, which GitHub also requires.
    """
    out: dict[str, dict[int, int]] = {"LEFT": {}, "RIGHT": {}}
    if not patch:
        return out
    left = right = 0
    hunk = -1
    for line in patch.rstrip("\n").split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            left, right = int(m.group(1)), int(m.group(2))
            hunk += 1
            continue
        if hunk < 0:
            continue
        marker = line[:1]
        if marker == "+":
            out["RIGHT"][right] = hunk
            right += 1
        elif marker == "-":
            out["LEFT"][left] = hunk
            left += 1
        elif marker == " " or line == "":
            out["RIGHT"][right] = hunk
            out["LEFT"][left] = hunk
            right += 1
            left += 1
        # "\ No newline at end of file" and anything else consume no line.
    return out


def partition_comments(
    comments: Iterable[Mapping[str, Any]],
    files: Mapping[str, str | None],
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
    """Split proposed comments into GitHub-acceptable inline ones and the rest.

    ``files`` maps each changed file's path to its unified patch (None for a
    binary or oversized file, which can carry no inline comment).

    Returns ``(inline, body)``. ``inline`` entries are ready for the create-
    review API (``path``, ``line``, ``side``, ``body`` and, for a valid range,
    ``start_line``/``start_side``). ``body`` holds the ORIGINAL mappings whose
    anchor the diff cannot carry, for the caller to list in the review body.
    """
    cache: dict[str, dict[str, dict[int, int]]] = {}
    inline: list[dict[str, Any]] = []
    body: list[Mapping[str, Any]] = []
    for c in comments:
        path = c.get("path") or ""
        line = c.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or path not in files:
            body.append(c)
            continue
        if path not in cache:
            cache[path] = commentable_lines(files[path])
        side: Side = "LEFT" if c.get("side") == "LEFT" else "RIGHT"
        hunk = cache[path][side].get(line)
        if hunk is None:
            body.append(c)
            continue
        out: dict[str, Any] = {"path": path, "line": line, "side": side, "body": c.get("body", "")}
        start = c.get("start_line")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and start < line
            and cache[path][side].get(start) == hunk
        ):
            out["start_line"] = start
            out["start_side"] = side
        inline.append(out)
    return inline, body


def split_by_severity(
    issues: Iterable[Mapping[str, Any]],
    inline_severities: Collection[str] = DEFAULT_INLINE_SEVERITIES,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """``(blocking, non_blocking)``. Unknown or missing severities are non-blocking."""
    wanted = {s.strip().lower() for s in inline_severities}
    blocking: list[Mapping[str, Any]] = []
    non_blocking: list[Mapping[str, Any]] = []
    for issue in issues:
        severity = str(issue.get("severity") or "").strip().lower()
        (blocking if severity in wanted else non_blocking).append(issue)
    return blocking, non_blocking


def format_issue_comment(issue: Mapping[str, Any]) -> str:
    """The text of one inline review comment."""
    return f"**{issue.get('severity', '')}: {issue.get('title', '')}**\n\n{issue.get('body', '')}"


def _list_item(issue: Mapping[str, Any]) -> str:
    path = issue.get("path") or ""
    line = issue.get("line")
    where = f"`{path}{f':{line}' if line else ''}` — " if path else ""
    text = str(issue.get("body") or "").strip().replace("\n", "\n  ")
    head = f"- {where}**{issue.get('severity', '')}: {issue.get('title', '')}**"
    return f"{head}\n\n  {text}" if text else head


def _prior_item(prior: Mapping[str, Any]) -> str:
    note = str(prior.get("note") or "").strip()
    item = f"- **{prior.get('status', 'unknown')}** — {prior.get('description', '')}"
    return f"{item}: {note}" if note else item


#: GitHub rejects a review body over 65,536 characters; stay well under it.
REVIEW_BODY_MAX_CHARS = 60_000


def _omitted(n: int) -> str:
    return f"…{n} more finding(s) omitted"


def _fit(items: list[str], room: int) -> tuple[list[str], int]:
    """The longest prefix of ``items`` (joined by blank lines) within ``room``."""
    kept: list[str] = []
    used = 0
    for item in items:
        cost = len(item) + 2
        if used + cost > room:
            break
        kept.append(item)
        used += cost
    return kept, len(items) - len(kept)


def compose_body(
    summary: str,
    out_of_diff: Iterable[Mapping[str, Any]],
    non_blocking: Iterable[Mapping[str, Any]],
    prior_issues: Iterable[Mapping[str, Any]] | None = None,
    *,
    footer: str = "",
    max_chars: int | None = REVIEW_BODY_MAX_CHARS,
) -> str:
    """The review body. Empty when there is nothing to say (a clean approval).

    Never longer than ``max_chars`` (None disables the cap). Over the cap, the
    non-blocking section is cut first, then the out-of-diff findings, and the
    summary last; each cut section ends with "…N more finding(s) omitted". The
    footer is never cut: it is the marker that makes a posted review findable.
    """
    summary_text = summary.strip() if summary else ""
    prior = [_prior_item(p) for p in (prior_issues or [])]
    major = [_list_item(i) for i in out_of_diff]
    minor = [_list_item(i) for i in non_blocking]
    if not (summary_text or prior or major or minor):
        return ""

    def build(sum_text: str, major_items: list[str], minor_items: list[str]) -> str:
        parts: list[str] = [sum_text] if sum_text else []
        if prior:
            parts += ["### Previous findings", *prior]
        if major_items:
            parts += ["### Other findings", *major_items]
        if minor_items:
            parts += ["### Non-blocking (for awareness)", *minor_items]
        if footer:
            parts.append(footer)
        return "\n\n".join(parts)

    body = build(summary_text, major, minor)
    if max_chars is None or len(body) <= max_chars:
        return body

    # Cut the non-blocking section first.
    base_len = len(build(summary_text, major, [])) + len("### Non-blocking (for awareness)") + 4
    room = max_chars - base_len - len(_omitted(len(minor))) - 4
    kept, dropped = _fit(minor, room)
    minor_out = [*kept, _omitted(dropped)] if dropped else kept
    body = build(summary_text, major, minor_out)
    if len(body) <= max_chars:
        return body

    # Then the out-of-diff findings.
    base_len = len(build(summary_text, [], [])) + len("### Other findings") + 4
    room = max_chars - base_len - len(_omitted(len(major))) - 4
    kept, dropped = _fit(major, room)
    major_out = [*kept, _omitted(dropped)] if dropped else kept
    minor_all = [_omitted(len(minor))] if minor else []
    body = build(summary_text, major_out, minor_all)
    if len(body) <= max_chars:
        return body

    # The summary goes last; a hard cut keeps the footer whatever else is huge.
    marker = "…(truncated)"
    room = max_chars - len(footer) - len(marker) - 4
    head = build(summary_text, major_out, minor_all)
    if footer:
        head = head[: -len(footer)].rstrip("\n")
    head = head[: max(room, 0)] + marker
    return f"{head}\n\n{footer}" if footer else head


_OWN_PR_PREFIX = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES REQUESTED"}


def own_pr_body(verdict: str, body: str) -> str:
    """The COMMENT body that carries a verdict GitHub will not let us submit."""
    prefix = _OWN_PR_PREFIX.get(verdict)
    if not prefix:
        return body
    return f"{prefix}\n\n{body}" if body else prefix


@dataclass(frozen=True)
class ReviewDecision:
    """What to do with a pull request at its current head."""

    action: Literal["review", "skip"]
    mode: Literal["full", "incremental"] | None = None
    since_sha: str | None = None
    reason: str | None = None


def decide_review(
    *,
    kind: Literal["initial", "rereview"],
    head_sha: str,
    last_reviewed_sha: str | None,
    completed_review_at_head: bool = False,
    compare_status: str | None = None,
) -> ReviewDecision:
    """Full, incremental, or skip — decided by SHA, never by asking the model.

    * A head that already has a completed review is never reviewed again.
    * A re-review after the head moved covers ``last_reviewed_sha..head_sha``.
    * ``compare_status`` is GitHub's answer for ``last_reviewed_sha...head_sha``
      (see ``github_compare``). ``diverged``/``behind``/``missing`` mean the
      history was rewritten, so the re-review falls back to a full one;
      ``identical`` means there is nothing new.
    """
    skip_reason = "no_new_commits" if kind == "rereview" else "already_reviewed"
    if completed_review_at_head or last_reviewed_sha == head_sha:
        return ReviewDecision(action="skip", reason=skip_reason)
    if kind == "rereview" and last_reviewed_sha:
        if compare_status == "identical":
            return ReviewDecision(action="skip", reason="no_new_commits")
        if compare_status in FULL_REVIEW_COMPARE_STATUSES:
            return ReviewDecision(action="review", mode="full")
        return ReviewDecision(action="review", mode="incremental", since_sha=last_reviewed_sha)
    return ReviewDecision(action="review", mode="full")
