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


def test_default_clone_root_follows_the_workspace(monkeypatch, tmp_path):
    from pathlib import Path

    from robothor.pr_review.config import ReviewerConfig

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
    cfg = ReviewerConfig(clone_root="")
    assert cfg.clone_dir("acme/widgets") == Path(
        tmp_path / "ws" / ".genus" / "pr-review" / "repos" / "acme" / "widgets"
    )


def test_requires_ticket_only_where_there_are_prefixes_to_match():
    from robothor.pr_review.config import ReviewerConfig

    cfg = ReviewerConfig(
        repos=("acme/widgets",),
        require_ticket=True,
        ticket_prefixes=("ABC",),
        repo_ticket_prefixes=(("other/tools", "TOOL"),),
    )
    assert cfg.requires_ticket("Acme/Widgets")  # listed: unchanged
    assert cfg.requires_ticket("other/tools")  # its own prefixes
    assert not cfg.requires_ticket("other/gadgets")  # neither: nothing to match
    assert not ReviewerConfig(repos=("acme/widgets",)).requires_ticket("acme/widgets")
