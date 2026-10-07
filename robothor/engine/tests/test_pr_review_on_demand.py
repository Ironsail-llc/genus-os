"""pr_review_intake(pr=...) and the ticket fetcher prepare is given: handler wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import pr_review
from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.store import MemoryStore
from robothor.pr_review.tests.fakes import REPO, FakeChat, FakeGitHub, FakeTasks, make_pr

_OWNER = ToolContext(agent_id="main", tenant_id="test-tenant", user_role="owner")


async def test_intake_with_a_pr_queues_just_that_pr_without_polling():
    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, "1" * 40))
    github.add(make_pr(8, "2" * 40))
    chat.post(f"https://github.com/{REPO}/pull/8", time="2099-01-01T00:00:00.000000Z")
    cfg = ReviewerConfig(repos=(REPO,), watch_repos=True, chat_space="spaces/AAAA")
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=store),
        patch.object(pr_review, "_github", return_value=github),
        patch.object(pr_review, "_chat", return_value=chat),
        patch.object(pr_review, "_tasks", return_value=tasks),
    ):
        result = await pr_review._intake({"pr": f"{REPO}#7"}, _OWNER)
    assert result["requested"]["status"] == "queued"
    assert [t["spec"].row.number for t in tasks.created] == [7]
    assert chat.list_calls == []


async def test_intake_with_a_pr_outside_the_repos_is_refused():
    cfg = ReviewerConfig(repos=(REPO,))
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=MemoryStore()),
        patch.object(pr_review, "_github", return_value=FakeGitHub()),
        patch.object(pr_review, "_tasks", return_value=FakeTasks()),
    ):
        result = await pr_review._intake({"pr": "https://github.com/other/x/pull/1"}, _OWNER)
    assert "error" in result


def test_the_intake_schema_offers_pr():
    from robothor.engine.tools.schemas import get_engine_schemas

    props = get_engine_schemas()["pr_review_intake"]["function"]["parameters"]["properties"]
    assert props["pr"]["type"] == "string"


async def test_intake_skip_action_stops_reviews_of_one_pr():
    store, github, tasks = MemoryStore(), FakeGitHub(), FakeTasks()
    github.add(make_pr(7, "1" * 40))
    cfg = ReviewerConfig(repos=(REPO,), watch_repos=True, chat_space="")
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=store),
        patch.object(pr_review, "_github", return_value=github),
        patch.object(pr_review, "_tasks", return_value=tasks),
    ):
        await pr_review._intake({"pr": f"{REPO}#7"}, _OWNER)
        result = await pr_review._intake({"pr": f"{REPO}#7", "action": "skip"}, _OWNER)
        bad = await pr_review._intake({"pr": f"{REPO}#7", "action": "bogus"}, _OWNER)
        missing = await pr_review._intake({"action": "skip"}, _OWNER)
    assert result["skipped"]["status"] == "closed"
    assert tasks.closed == ["task-1"]
    assert "error" in bad and "error" in missing


def test_the_intake_schema_offers_the_skip_action():
    from robothor.engine.tools.schemas import get_engine_schemas

    props = get_engine_schemas()["pr_review_intake"]["function"]["parameters"]["properties"]
    assert props["action"]["enum"] == ["review", "skip"]


async def test_the_ticket_fetcher_is_none_without_jira(monkeypatch):
    for name in ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with patch("robothor.secrets.get_secret", return_value=None):
        fetch = pr_review._ticket_fetcher(_OWNER)
        assert await fetch("ABC-1") is None


async def test_the_ticket_fetcher_calls_the_jira_handler_with_include_text(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "bot@example.com")
    handler = AsyncMock(return_value={"key": "ABC-1", "summary": "s"})
    with (
        patch("robothor.secrets.get_secret", return_value="tok"),
        patch("robothor.engine.tools.handlers.jira._jira_get_issue", handler),
    ):
        out = await pr_review._ticket_fetcher(_OWNER)("ABC-1")
    assert out == {"key": "ABC-1", "summary": "s"}
    assert handler.call_args.args[0] == {"issue_key": "ABC-1", "include_text": True}
