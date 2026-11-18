"""Paired reviewers: a second automated reviewer on the same pull request.

Two review bots may be asked for independent opinions on one pull request.
Each must review blind (nothing the peer wrote reaches its prompt) and must
not treat the peer's thread replies as a person answering a finding.
"""

from __future__ import annotations

from robothor.pr_review import context as ctxmod
from robothor.pr_review.config import ReviewerConfig, load_config
from robothor.pr_review.intake import replies_pending
from robothor.pr_review.store import PrReviewRow
from robothor.pr_review.tests.fakes import REPO, make_pr
from robothor.pr_review.tests.test_review_context import SHA, TENANT, RichGitHub, _prepare

US, PEER = "genus-bot", "peer-bot"


def _user(login: str) -> dict[str, str]:
    return {"login": login, "type": "User"}


def _thread(last_login: str) -> list[dict]:
    return [
        {"id": 77, "user": _user(US), "body": "missing retry", "created_at": "2026-10-01T00:00:00Z"},
        {
            "id": 78,
            "in_reply_to_id": 77,
            "user": _user(last_login),
            "body": "a reply on the thread",
            "created_at": "2026-10-02T00:00:00Z",
        },
    ]


def test_peer_logins_come_from_the_setting_lowercased(monkeypatch):
    assert ReviewerConfig().peer_logins == ()
    monkeypatch.setenv("ROBOTHOR_PR_REVIEW_PEER_LOGINS", "Peer-Bot, other-bot ,")
    from robothor.settings import reset_settings

    reset_settings()
    try:
        assert load_config().peer_logins == ("peer-bot", "other-bot")
    finally:
        monkeypatch.delenv("ROBOTHOR_PR_REVIEW_PEER_LOGINS")
        reset_settings()


def test_a_peer_reply_is_not_a_pending_reply_but_a_persons_is():
    assert ctxmod.pending_replies(_thread(PEER), [77], exclude_logins=(PEER,)) == []
    assert ctxmod.pending_replies(_thread("alice"), [77], exclude_logins=(PEER,)) == [77]
    # Login case never lets a peer through.
    assert ctxmod.pending_replies(_thread("Peer-Bot"), [77], exclude_logins=(PEER,)) == []


def test_a_peer_reply_is_left_out_of_the_thread_shown_to_the_reviewer():
    comments = [
        *_thread(PEER),
        {
            "id": 79,
            "in_reply_to_id": 77,
            "user": _user("alice"),
            "body": "out of scope",
            "created_at": "2026-10-03T00:00:00Z",
        },
    ]
    replies = ctxmod.thread_replies(comments, exclude_logins=(PEER,))
    assert [r["author"] for r in replies[77]] == ["alice"]


async def test_a_peer_reply_on_our_finding_queues_no_rereview():
    row = PrReviewRow(
        tenant_id=TENANT,
        repo=REPO,
        number=7,
        status="reviewed",
        last_review={"issues": [{"comment_id": 77}]},
    )
    gh = RichGitHub()
    gh.review_comments = _thread(PEER)
    assert await replies_pending(gh, row, peer_logins=(PEER,)) is False
    gh.review_comments = _thread("alice")
    assert await replies_pending(gh, row, peer_logins=(PEER,)) is True


async def test_the_review_is_blind_to_what_a_peer_wrote(tmp_path):
    gh = RichGitHub()
    gh.add(make_pr(7, SHA))
    gh.reviews = [
        {
            "user": _user(PEER),
            "state": "CHANGES_REQUESTED",
            "body": "PEER-VERDICT-TEXT",
            "submitted_at": "2026-10-01T00:00:00Z",
        },
        {
            "user": _user("carol"),
            "state": "APPROVED",
            "body": "looks right to me",
            "submitted_at": "2026-10-01T01:00:00Z",
        },
    ]
    gh.review_comments = [
        {
            "user": _user(PEER),
            "path": "src/a.py",
            "line": 1,
            "body": "PEER-FINDING-TEXT",
            "created_at": "2026-10-01T00:00:00Z",
        }
    ]
    gh.issue_comments = [
        {"user": _user(PEER), "body": "PEER-COMMENT-TEXT", "created_at": "2026-10-01T00:00:00Z"}
    ]
    cfg = ReviewerConfig(repos=(REPO,), bot_login=US, peer_logins=(PEER,))
    _, [args] = await _prepare(gh, tmp_path, cfg=cfg)
    task = args["task"]
    assert "looks right to me" in task
    for leaked in ("PEER-VERDICT-TEXT", "PEER-FINDING-TEXT", "PEER-COMMENT-TEXT", PEER):
        assert leaked not in task
