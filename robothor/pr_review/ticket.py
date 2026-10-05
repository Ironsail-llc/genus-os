"""The linked ticket, fetched once by prepare and handed to the review as data.

A review checks the pull request against its ticket's acceptance criteria.
The review job has no network, so :func:`ticket_context` fetches the ticket
before the job starts — through the instance's Jira integration
(``jira_get_issue`` with ``include_text``), never through a model — and turns
it into a :class:`~robothor.pr_review.prompt.TicketContext`: size-capped and
passed through the secret redactor, since ticket text is posted nowhere but
is still read by a model that may quote it into a public review.

A fetcher returns the issue dict, a dict with ``error``, or None when no
ticket system is configured. Each of those is said plainly in the prompt; the
model is told never to invent criteria it was not given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.pr_review.prompt import TicketContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    TicketFetcher = Callable[[str], Awaitable[dict[str, Any] | None]]

__all__ = ["TICKET_TEXT_MAX", "ticket_context"]

#: Characters of ticket text (acceptance criteria first, then description).
TICKET_TEXT_MAX = 8000
_ERROR_MAX = 300


def _redact(text: Any) -> str:
    from robothor.secrets.redaction import redact

    return redact(str(text or ""))


async def ticket_context(
    key: str, fetch: TicketFetcher | None, *, max_chars: int = TICKET_TEXT_MAX
) -> TicketContext:
    if fetch is None:
        return TicketContext(key=key, state="not_configured")
    try:
        data = await fetch(key)
    except Exception as exc:  # noqa: BLE001 - the review goes ahead without the ticket
        return TicketContext(
            key=key, state="unavailable", error=_redact(f"{type(exc).__name__}: {exc}")[:_ERROR_MAX]
        )
    if data is None:
        return TicketContext(key=key, state="not_configured")
    if data.get("error"):
        return TicketContext(
            key=key,
            state="unavailable",
            url=str(data.get("url") or ""),
            error=_redact(data["error"])[:_ERROR_MAX],
        )
    criteria = _redact(data.get("acceptance_criteria")).strip()
    description = _redact(data.get("description")).strip()
    truncated = False
    if len(criteria) > max_chars:
        criteria, description, truncated = criteria[:max_chars], "", True
    room = max_chars - len(criteria)
    if len(description) > room:
        description, truncated = description[:room], True
    return TicketContext(
        key=str(data.get("key") or key),
        state="fetched",
        url=str(data.get("url") or ""),
        summary=_redact(data.get("summary"))[:500],
        status=str(data.get("status") or ""),
        issue_type=str(data.get("issue_type") or ""),
        description=description,
        acceptance_criteria=criteria,
        truncated=truncated,
    )
