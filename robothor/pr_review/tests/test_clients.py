"""The GitHub client's review-request search asks for DIRECT requests only."""

from __future__ import annotations

from typing import Any

from robothor.pr_review.clients import GitHubClient


async def test_review_requested_matches_direct_requests_not_team_requests(monkeypatch):
    """Observed 2026-10-05: ``review-requested:<login>`` also matches a review
    requested from any team the login belongs to. The bot sits in an org team
    requested on many old pull requests, so the intake queued months-old
    reviews nobody had asked it for. ``user-review-requested:`` is the
    direct-request qualifier."""
    client = GitHubClient(token="test-token")
    seen: list[dict[str, Any]] = []

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        seen.append({"path": path, **(params or {})})
        return {
            "items": [
                {"repository_url": "https://api.github.com/repos/acme/widgets", "number": 7}
            ]
        }

    monkeypatch.setattr(client, "_get", fake_get)
    assert await client.review_requested("review-bot") == [("acme/widgets", 7)]
    [call] = seen
    assert call["path"] == "/search/issues"
    terms = call["q"].split()
    assert "user-review-requested:review-bot" in terms
    assert not [t for t in terms if t.startswith("review-requested:")]
    assert {"is:pr", "is:open", "archived:false"} <= set(terms)
