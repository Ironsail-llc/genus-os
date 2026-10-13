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


def test_three_reviews_run_at_once_with_a_queue_ready_behind_them(monkeypatch):
    from robothor.pr_review.config import ReviewerConfig, load_config
    from robothor.settings.model import PrReviewSettings

    assert PrReviewSettings().workers == 3 and ReviewerConfig().workers == 3
    assert PrReviewSettings().max_concurrent == 6 and ReviewerConfig().max_concurrent == 6
    monkeypatch.setenv("ROBOTHOR_PR_REVIEW_WORKERS", "5")
    from robothor.settings import reset_settings

    reset_settings()
    try:
        assert load_config().workers == 5
    finally:
        monkeypatch.delenv("ROBOTHOR_PR_REVIEW_WORKERS")
        reset_settings()


def test_the_status_file_defaults_into_the_workspace_memory(monkeypatch, tmp_path):
    from robothor.pr_review.config import load_config
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
    reset_settings()
    try:
        assert load_config().status_file == str(
            tmp_path / "ws" / "brain" / "memory" / "pr-reviewer-status.md"
        )
    finally:
        monkeypatch.delenv("ROBOTHOR_WORKSPACE")
        reset_settings()
