"""Reviewer configuration defaults."""

from __future__ import annotations


def test_watch_repos_is_off_by_default():
    """Enabling the suite must not review every open PR in the configured repos.

    The default is the reference bot's behaviour: only PRs posted in the Chat
    space or requesting the bot's review are picked up.
    """
    from robothor.pr_review.config import ReviewerConfig
    from robothor.settings.model import PrReviewSettings

    assert ReviewerConfig().watch_repos is False
    assert PrReviewSettings().watch_repos is False
