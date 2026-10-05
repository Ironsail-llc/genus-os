"""GitHub diff and review-posting tools, against a recorded-shape fake GitHub.

No test here reaches the network: every handler's client is built by
``github_api._client``, which these tests replace with one whose transport is
an in-process fake serving fixtures in GitHub's own response shapes.

What is pinned, beyond "it posts":

* An anchor outside the diff moves into the body instead of 422-ing the review.
* GitHub refuses APPROVE / REQUEST_CHANGES on the token user's own pull
  request; the verdict is posted as a COMMENT whose first line states it —
  decided up front when the author is known, and again on the 422 when not.
* A 422 caused by an anchor is retried once with every finding in the body.
* Thread resolution touches ONLY threads opened by the review ids passed in.
  The bot this was ported from resolved every thread its account had started,
  including ones a person wrote by hand from the same account.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import github_api

_CTX = ToolContext(agent_id="pr-reviewer", tenant_id="test-tenant")
_REPO = "acme/widgets"
_HEAD = "b" * 40

PATCH = "\n".join(
    [
        "@@ -10,4 +10,5 @@ def a():",
        " ctx10",
        "-old11",
        "+new11",
        "+new12",
        " ctx12",
    ]
)

PR_FILES = [
    {
        "filename": "src/a.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
        "patch": PATCH,
    },
    {
        "filename": "assets/logo.png",
        "status": "added",
        "additions": 0,
        "deletions": 0,
        "changes": 0,
    },
]


def _pr(author: str = "alice", head: str = _HEAD) -> dict[str, Any]:
    return {
        "number": 7,
        "state": "open",
        "title": "Add widget",
        "user": {"login": author},
        "head": {"sha": head, "ref": "feature"},
        "base": {"sha": "a" * 40, "ref": "main"},
        "html_url": f"https://github.com/{_REPO}/pull/7",
    }


class FakeGitHub:
    """Routes requests to fixture responses and records what was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.viewer: str | None = "robo-bot"
        self.pr = _pr()
        self.files = PR_FILES
        self.diff = "diff --git a/src/a.py b/src/a.py\n" + PATCH
        self.review_responses: list[httpx.Response] = []
        self.review_comments = [
            {"id": 9001, "path": "src/a.py", "line": 11, "body": "x"},
        ]
        self.compare: dict[str, Any] | None = None
        self.threads_pages: list[dict[str, Any]] = []
        self.resolved: list[str] = []
        self.existing_reviews: list[dict[str, Any]] = []
        self.post_error: Exception | None = None
        self.pr_reads = 0
        self.pr_after_first_read: dict[str, Any] | None = None
        self.repo_status = 200
        self.commit_status = 200
        self.comment_pages: list[list[dict[str, Any]]] | None = None

    # ── helpers ──
    def posted_reviews(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path.endswith("/reviews")
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        base = f"/repos/{_REPO}"
        if path == "/user":
            if self.viewer is None:
                return httpx.Response(403, json={"message": "Resource not accessible"})
            return httpx.Response(200, json={"login": self.viewer})
        if path == f"{base}/pulls/7" and request.method == "GET":
            if "diff" in request.headers.get("Accept", ""):
                return httpx.Response(200, text=self.diff)
            self.pr_reads += 1
            if self.pr_after_first_read is not None and self.pr_reads > 1:
                return httpx.Response(200, json=self.pr_after_first_read)
            return httpx.Response(200, json=self.pr)
        if path == base:
            return httpx.Response(self.repo_status, json={"full_name": _REPO})
        if path.startswith(f"{base}/commits/"):
            return httpx.Response(self.commit_status, json={"sha": path.split("/")[-1]})
        if path == f"{base}/pulls/7/reviews" and request.method == "GET":
            return httpx.Response(200, json=self.existing_reviews)
        if path == f"{base}/pulls/404":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"{base}/pulls/7/files":
            page = int(request.url.params.get("page", "1"))
            if page == 1 and len(self.files) > 1:
                link = (
                    f'<https://api.github.com{base}/pulls/7/files?per_page=100&page=2>; rel="next"'
                )
                return httpx.Response(200, json=self.files[:1], headers={"Link": link})
            return httpx.Response(200, json=self.files[1:] if page == 2 else self.files)
        if path == f"{base}/pulls/7/reviews" and request.method == "POST":
            if self.post_error is not None:
                raise self.post_error
            if self.review_responses:
                return self.review_responses.pop(0)
            return _review_ok()
        if path.startswith(f"{base}/pulls/7/reviews/") and path.endswith("/comments"):
            if self.comment_pages is not None:
                page = int(request.url.params.get("page", "1"))
                hdrs = {}
                if page < len(self.comment_pages):
                    nxt = f"https://api.github.com{path}?per_page=100&page={page + 1}"
                    hdrs["Link"] = f'<{nxt}>; rel="next"'
                return httpx.Response(200, json=self.comment_pages[page - 1], headers=hdrs)
            return httpx.Response(200, json=self.review_comments)
        if path.startswith(f"{base}/pulls/7/comments/") and path.endswith("/replies"):
            return httpx.Response(
                201,
                json={
                    "id": 9100,
                    "html_url": f"https://github.com/{_REPO}/pull/7#discussion_r9100",
                    "in_reply_to_id": int(path.split("/")[-2]),
                },
            )
        if path.startswith(f"{base}/compare/"):
            if self.compare is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self.compare)
        if path == "/graphql":
            payload = json.loads(request.content)
            if "resolveReviewThread" in payload["query"]:
                tid = payload["variables"]["id"]
                self.resolved.append(tid)
                return httpx.Response(
                    200,
                    json={
                        "data": {"resolveReviewThread": {"thread": {"id": tid, "isResolved": True}}}
                    },
                )
            after = payload["variables"].get("after")
            idx = 0 if after is None else int(after)
            return httpx.Response(200, json=self.threads_pages[idx])
        return httpx.Response(500, json={"message": f"unrouted {request.method} {path}"})


def _review_ok(review_id: int = 555) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": review_id,
            "state": "COMMENTED",
            "html_url": f"https://github.com/{_REPO}/pull/7#pullrequestreview-{review_id}",
        },
    )


def _422(message: str, errors: list[Any] | None = None) -> httpx.Response:
    return httpx.Response(
        422, json={"message": "Unprocessable Entity", "errors": errors or [message]}
    )


@pytest.fixture
def gh():
    fake = FakeGitHub()

    def factory(timeout: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), timeout=timeout)

    with (
        patch.object(github_api, "_get_token", return_value="ghp_test"),
        patch.object(github_api, "_client", side_effect=factory),
    ):
        yield fake


def _issue(severity: str, title: str, line: int | None, **kw: Any) -> dict[str, Any]:
    out = {
        "path": "src/a.py",
        "line": line,
        "side": "RIGHT",
        "severity": severity,
        "title": title,
        "body": f"{title} detail",
    }
    out.update(kw)
    return out


# ─── Read tools ────────────────────────────────────────────────────


class TestPrDiff:
    async def test_returns_the_unified_diff(self, gh):
        result = await github_api._github_pr_diff({"repo": _REPO, "number": 7}, _CTX)
        assert result["diff"].startswith("diff --git")
        assert result["truncated"] is False
        req = gh.requests[-1]
        assert req.headers["Accept"] == "application/vnd.github.diff"

    async def test_truncates_a_huge_diff_and_says_so(self, gh):
        gh.diff = "+" * (github_api._DIFF_MAX_CHARS + 50)
        result = await github_api._github_pr_diff({"repo": _REPO, "number": 7}, _CTX)
        assert result["truncated"] is True
        assert len(result["diff"]) == github_api._DIFF_MAX_CHARS
        assert result["total_chars"] == github_api._DIFF_MAX_CHARS + 50

    async def test_missing_pr(self, gh):
        result = await github_api._github_pr_diff({"repo": _REPO, "number": 404}, _CTX)
        assert "not found" in result["error"]

    async def test_requires_repo_and_number(self, gh):
        result = await github_api._github_pr_diff({"repo": _REPO}, _CTX)
        assert "error" in result

    async def test_rejects_a_malformed_repo(self, gh):
        result = await github_api._github_pr_diff({"repo": "../etc", "number": 7}, _CTX)
        assert "owner/repo" in result["error"]
        assert gh.requests == []


class TestPrFiles:
    async def test_lists_every_page_with_patches(self, gh):
        result = await github_api._github_pr_files({"repo": _REPO, "number": 7}, _CTX)
        assert [f["filename"] for f in result["files"]] == ["src/a.py", "assets/logo.png"]
        first = result["files"][0]
        assert (first["status"], first["additions"], first["deletions"]) == ("modified", 2, 1)
        assert first["patch"] == PATCH
        assert result["files"][1]["patch"] is None
        assert result["count"] == 2


class TestCompare:
    async def test_ahead_is_incremental(self, gh):
        gh.compare = {
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "total_commits": 2,
            "commits": [
                {
                    "sha": "c1",
                    "commit": {"message": "fix: guard\n\nbody", "author": {"name": "Bob"}},
                },
            ],
            "files": PR_FILES[:1],
        }
        result = await github_api._github_compare(
            {"repo": _REPO, "base": "a" * 40, "head": _HEAD}, _CTX
        )
        assert result["status"] == "ahead"
        assert result["full_review_required"] is False
        assert result["commits"] == [{"sha": "c1", "message": "fix: guard", "author": "Bob"}]
        assert result["files"][0]["filename"] == "src/a.py"
        assert gh.requests[-1].url.path == f"/repos/{_REPO}/compare/{'a' * 40}...{_HEAD}"

    @pytest.mark.parametrize("status", ["diverged", "behind"])
    async def test_rewritten_history_forces_a_full_review(self, gh, status):
        gh.compare = {"status": status, "ahead_by": 1, "behind_by": 3, "commits": [], "files": []}
        result = await github_api._github_compare({"repo": _REPO, "base": "x", "head": "y"}, _CTX)
        assert result["full_review_required"] is True

    async def test_a_vanished_base_is_missing_and_full(self, gh):
        result = await github_api._github_compare(
            {"repo": _REPO, "base": "gone", "head": "y"}, _CTX
        )
        assert result["status"] == "missing"
        assert result["full_review_required"] is True

    async def test_compare_status_feeds_decide_review(self, gh):
        from robothor.pr_review.posting import decide_review

        gh.compare = {"status": "diverged", "commits": [], "files": []}
        cmp = await github_api._github_compare({"repo": _REPO, "base": "a", "head": "b"}, _CTX)
        d = decide_review(
            kind="rereview", head_sha="b", last_reviewed_sha="a", compare_status=cmp["status"]
        )
        assert d.mode == "full"


# ─── github_create_review ──────────────────────────────────────────


def _review_args(verdict: str, issues: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    args = {
        "repo": _REPO,
        "number": 7,
        "verdict": verdict,
        "summary": "Summary text.",
        "issues": issues,
        "commit_id": _HEAD,
    }
    args.update(kw)
    return args


class TestCreateReview:
    async def test_partitions_anchors_and_splits_severity(self, gh):
        issues = [
            _issue("blocker", "In diff", 11),
            _issue("major", "Outside diff", 99),
            _issue("major", "Left side", 11, side="LEFT"),
            _issue("minor", "Naming", 12),
        ]
        result = await github_api._github_create_review(
            _review_args("REQUEST_CHANGES", issues), _CTX
        )

        [posted] = gh.posted_reviews()
        assert posted["event"] == "REQUEST_CHANGES"
        assert posted["commit_id"] == _HEAD
        assert [(c["line"], c["side"]) for c in posted["comments"]] == [
            (11, "RIGHT"),
            (11, "LEFT"),
        ]
        assert posted["comments"][0]["body"].startswith("**blocker: In diff**")
        assert "### Other findings" in posted["body"]
        assert "Outside diff" in posted["body"]
        assert "### Non-blocking (for awareness)" in posted["body"]
        assert "Naming" in posted["body"]
        # A minor finding never becomes an inline thread, even when anchorable.
        assert all("Naming" not in c["body"] for c in posted["comments"])

        assert result["review_id"] == 555
        assert result["url"].endswith("#pullrequestreview-555")
        assert result["event"] == "REQUEST_CHANGES"
        assert result["inline_count"] == 2
        assert result["body_findings"] == 2
        assert result["comment_ids"] == [9001]
        assert result["posted_as_comment"] is False

    async def test_approve_with_a_blocking_finding_is_never_posted_as_approve(self, gh):
        """Every caller crosses this guard, not only the pr-reviewer's finalize."""
        result = await github_api._github_create_review(
            _review_args("APPROVE", [_issue("blocker", "In diff", 11)]), _CTX
        )
        [posted] = gh.posted_reviews()
        assert posted["event"] == "REQUEST_CHANGES"
        assert result["verdict"] == "REQUEST_CHANGES"
        assert result["verdict_overridden"] is True

    async def test_approve_with_only_minor_findings_stays_approve(self, gh):
        result = await github_api._github_create_review(
            _review_args("APPROVE", [_issue("minor", "Naming", 12)]), _CTX
        )
        assert gh.posted_reviews()[0]["event"] == "APPROVE"
        assert result["verdict_overridden"] is False

    async def test_returns_the_inline_comments_with_their_anchors(self, gh):
        result = await github_api._github_create_review(
            _review_args("REQUEST_CHANGES", [_issue("blocker", "In diff", 11)]), _CTX
        )
        assert result["comments"] == [{"id": 9001, "path": "src/a.py", "line": 11}]

    async def test_multi_line_range_is_posted(self, gh):
        issues = [_issue("blocker", "Range", 13, start_line=11)]
        await github_api._github_create_review(_review_args("COMMENT", issues), _CTX)
        [posted] = gh.posted_reviews()
        assert posted["comments"][0]["start_line"] == 11
        assert posted["comments"][0]["start_side"] == "RIGHT"

    async def test_prior_issues_reach_the_body(self, gh):
        prior = [{"description": "Null check", "status": "resolved", "note": "Fixed"}]
        await github_api._github_create_review(
            _review_args("APPROVE", [], prior_issues=prior), _CTX
        )
        [posted] = gh.posted_reviews()
        assert "### Previous findings" in posted["body"]
        assert "**resolved** — Null check: Fixed" in posted["body"]

    @pytest.mark.parametrize(
        ("verdict", "prefix", "severity"),
        [("APPROVE", "APPROVED", "minor"), ("REQUEST_CHANGES", "CHANGES REQUESTED", "blocker")],
    )
    async def test_own_pr_posts_the_verdict_as_a_comment(self, gh, verdict, prefix, severity):
        gh.pr = _pr(author="Robo-Bot")  # case differs: logins compare case-insensitively
        result = await github_api._github_create_review(
            _review_args(verdict, [_issue(severity, "In diff", 11)]), _CTX
        )
        [posted] = gh.posted_reviews()
        assert posted["event"] == "COMMENT"
        assert posted["body"].startswith(f"{prefix}\n\n")
        assert result["event"] == "COMMENT"
        assert result["verdict"] == verdict
        assert result["posted_as_comment"] is True

    async def test_422_on_an_approve_falls_back_to_a_comment(self, gh):
        """An app token cannot read /user, so the author check is skipped; GitHub then refuses."""
        gh.viewer = None
        gh.pr = _pr(author="robo-bot")
        gh.review_responses = [_422("Can not approve your own pull request")]
        result = await github_api._github_create_review(_review_args("APPROVE", []), _CTX)

        first, second = gh.posted_reviews()
        assert first["event"] == "APPROVE"
        assert second["event"] == "COMMENT"
        assert second["body"].startswith("APPROVED")
        assert result["posted_as_comment"] is True
        assert result["review_id"] == 555

    async def test_422_on_a_bad_anchor_retries_once_with_everything_in_the_body(self, gh):
        gh.review_responses = [
            _422("x", errors=["Line could not be resolved"]),
        ]
        result = await github_api._github_create_review(
            _review_args("REQUEST_CHANGES", [_issue("blocker", "In diff", 11)]), _CTX
        )
        first, second = gh.posted_reviews()
        assert len(first["comments"]) == 1
        assert second["comments"] == []
        assert second["event"] == "REQUEST_CHANGES"
        assert "### Other findings" in second["body"]
        assert "In diff" in second["body"]
        assert result["inline_count"] == 0
        assert result["anchors_folded"] is True
        assert result["comment_ids"] == []

    async def test_a_second_422_is_reported_not_retried_forever(self, gh):
        gh.review_responses = [
            _422("x", errors=["Line could not be resolved"]),
            _422("x", errors=["Something else"]),
        ]
        result = await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "In diff", 11)]), _CTX
        )
        assert "error" in result
        assert "422" in result["error"]
        assert len(gh.posted_reviews()) == 2

    async def test_refuses_when_the_head_moved(self, gh):
        result = await github_api._github_create_review(
            _review_args("APPROVE", [], commit_id="c" * 40), _CTX
        )
        assert "head" in result["error"]
        assert result["head_sha"] == _HEAD
        assert gh.posted_reviews() == []

    async def test_comment_with_nothing_to_say_still_has_a_body(self, gh):
        await github_api._github_create_review(_review_args("COMMENT", [], summary=""), _CTX)
        [posted] = gh.posted_reviews()
        assert posted["body"]

    async def test_rejects_an_unknown_verdict(self, gh):
        result = await github_api._github_create_review(_review_args("LGTM", []), _CTX)
        assert "verdict" in result["error"]
        assert gh.requests == []

    async def test_rejects_malformed_issues(self, gh):
        result = await github_api._github_create_review(_review_args("COMMENT", ["oops"]), _CTX)
        assert "issues" in result["error"]

    async def test_refused_on_a_benchmark_run(self, gh):
        ctx = ToolContext(agent_id="probe", tenant_id="test-tenant", is_benchmark=True)
        result = await github_api._github_create_review(_review_args("APPROVE", []), ctx)
        assert "benchmark" in result["error"]
        assert gh.requests == []


# ─── github_reply_review_comment ───────────────────────────────────


class TestReplyReviewComment:
    async def test_replies_in_the_thread(self, gh):
        result = await github_api._github_reply_review_comment(
            {"repo": _REPO, "number": 7, "comment_id": 9001, "body": "Fixed, thanks."}, _CTX
        )
        req = gh.requests[-1]
        assert req.method == "POST"
        assert req.url.path == f"/repos/{_REPO}/pulls/7/comments/9001/replies"
        assert json.loads(req.content) == {"body": "Fixed, thanks."}
        assert result["comment_id"] == 9100
        assert result["in_reply_to_id"] == 9001

    async def test_requires_a_body(self, gh):
        result = await github_api._github_reply_review_comment(
            {"repo": _REPO, "number": 7, "comment_id": 9001, "body": " "}, _CTX
        )
        assert "error" in result
        assert gh.requests == []


# ─── github_resolve_threads ────────────────────────────────────────


def _thread(tid: str, review_id: int | None, *, resolved: bool = False, login: str = "robo-bot"):
    review = {"databaseId": review_id} if review_id is not None else None
    return {
        "id": tid,
        "isResolved": resolved,
        "comments": {
            "nodes": [{"databaseId": 1, "author": {"login": login}, "pullRequestReview": review}]
        },
    }


def _threads_page(nodes: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": next_cursor is not None,
                            "endCursor": next_cursor,
                        },
                        "nodes": nodes,
                    }
                }
            }
        }
    }


class TestResolveThreads:
    async def test_resolves_only_threads_our_reviews_opened(self, gh):
        gh.threads_pages = [
            _threads_page(
                [
                    _thread("T_ours", 555),
                    # Same account, but a person wrote it by hand: a different review.
                    _thread("T_handwritten", 777),
                    _thread("T_other_user", 888, login="bob"),
                ],
                next_cursor="1",
            ),
            _threads_page(
                [
                    _thread("T_ours_page2", 556),
                    _thread("T_already", 555, resolved=True),
                    _thread("T_no_review", None),
                ],
                next_cursor=None,
            ),
        ]
        result = await github_api._github_resolve_threads(
            {"repo": _REPO, "number": 7, "review_ids": [555, 556]}, _CTX
        )
        assert gh.resolved == ["T_ours", "T_ours_page2"]
        assert "T_handwritten" not in gh.resolved
        assert result["resolved"] == 2
        assert result["thread_ids"] == ["T_ours", "T_ours_page2"]
        assert result["already_resolved"] == 1

    async def test_refuses_without_review_ids(self, gh):
        result = await github_api._github_resolve_threads(
            {"repo": _REPO, "number": 7, "review_ids": []}, _CTX
        )
        assert "review_ids" in result["error"]
        assert gh.requests == []

    async def test_graphql_errors_are_reported(self, gh):
        gh.threads_pages = [{"errors": [{"message": "Could not resolve to a Repository"}]}]
        result = await github_api._github_resolve_threads(
            {"repo": _REPO, "number": 7, "review_ids": [1]}, _CTX
        )
        assert "Could not resolve" in result["error"]
        assert gh.resolved == []


# ─── Registration and classification ───────────────────────────────

READ_TOOLS = ("github_pr_diff", "github_pr_files", "github_compare")
WRITE_TOOLS = ("github_create_review", "github_reply_review_comment", "github_resolve_threads")


class TestRegistration:
    def test_handlers_are_registered(self):
        for name in READ_TOOLS + WRITE_TOOLS:
            assert name in github_api.HANDLERS

    def test_schemas_exist(self):
        from robothor.engine.tools.schemas import get_engine_schemas

        schemas = get_engine_schemas()
        for name in READ_TOOLS + WRITE_TOOLS:
            assert name in schemas, name

    def test_read_tools_are_read_only_and_write_tools_are_not(self):
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert set(READ_TOOLS) <= READONLY_TOOLS
        assert not set(WRITE_TOOLS) & READONLY_TOOLS

    def test_write_tools_are_opt_in(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.tools.constants import OPT_IN_TOOLS
        from robothor.engine.tools.registry import ToolRegistry

        assert set(WRITE_TOOLS) <= OPT_IN_TOOLS
        registry = ToolRegistry()
        default = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))
        assert not set(WRITE_TOOLS) & default
        assert set(READ_TOOLS) <= default
        granted = set(
            registry.get_tool_names(
                AgentConfig(id="reviewer", name="Reviewer", tools_allowed=list(WRITE_TOOLS))
            )
        )
        assert set(WRITE_TOOLS) <= granted

    def test_tools_have_search_keywords(self):
        from robothor.engine.tools.keywords import TOOL_HINTS

        for name in READ_TOOLS + WRITE_TOOLS:
            assert "review" in TOOL_HINTS[name].keywords or "diff" in TOOL_HINTS[name].keywords


# ─── review hardening ──────────────────────────────────────────────


def _existing(review_id: int = 321, commit: str = _HEAD, user: str = "robo-bot", body: str = ""):
    return {
        "id": review_id,
        "user": {"login": user},
        "commit_id": commit,
        "body": body or f"hello\n\n{github_api._REVIEW_FOOTER}",
        "html_url": f"https://github.com/{_REPO}/pull/7#pullrequestreview-{review_id}",
    }


class TestIdempotentPosting:
    async def test_existing_review_at_head_is_not_posted_again(self, gh):
        gh.existing_reviews = [_existing()]
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert result["already_posted"] is True
        assert result["review_id"] == 321
        assert "pullrequestreview-321" in result["url"]
        assert gh.posted_reviews() == []

    async def test_already_posted_returns_the_reviews_inline_comments(self, gh):
        """A retried post must still hand back comment ids, or re-reviews lose our threads."""
        gh.existing_reviews = [_existing()]
        gh.review_comments = [
            {"id": 11, "path": "app.py", "line": 3},
            {"id": 12, "path": "app.py", "line": 9},
        ]
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert result["already_posted"] is True
        assert result["comment_ids"] == [11, 12]
        assert result["comments"] == [
            {"id": 11, "path": "app.py", "line": 3},
            {"id": 12, "path": "app.py", "line": 9},
        ]

    @pytest.mark.parametrize(
        "review",
        [
            _existing(commit="c" * 40),
            _existing(user="someone-else"),
            _existing(body="a human wrote this"),
        ],
    )
    async def test_non_matching_reviews_do_not_block(self, gh, review):
        gh.existing_reviews = [review]
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert result["already_posted"] is False
        assert len(gh.posted_reviews()) == 1

    async def test_timeout_on_post_returns_the_review_that_landed(self, gh):
        gh.post_error = httpx.ReadTimeout("slow")
        gh.existing_reviews = []

        # The review appears server-side only after the (timed out) POST.
        orig = gh.handler

        def handler(request: httpx.Request) -> httpx.Response:
            resp = None
            try:
                resp = orig(request)
            finally:
                if request.method == "POST":
                    gh.existing_reviews = [_existing()]
            return resp

        gh.handler = handler  # type: ignore[method-assign]
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert result["already_posted"] is True
        assert result["review_id"] == 321

    async def test_timeout_with_no_review_reports_failure(self, gh):
        gh.post_error = httpx.ReadTimeout("slow")
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert "error" in result

    async def test_5xx_on_post_checks_for_the_review(self, gh):
        gh.review_responses = [httpx.Response(502, json={"message": "bad gateway"})]
        orig = gh.handler

        def handler(request: httpx.Request) -> httpx.Response:
            resp = orig(request)
            if request.method == "POST":
                gh.existing_reviews = [_existing()]
            return resp

        gh.handler = handler  # type: ignore[method-assign]
        result = await github_api._github_create_review(_review_args("COMMENT", []), _CTX)
        assert result["already_posted"] is True


class TestBodyLimits:
    async def test_body_is_capped_and_non_blocking_truncated_first(self, gh):
        minors = [_issue("minor", f"m{i}", None, body="x" * 2000) for i in range(100)]
        result = await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "keep me", 11), *minors]), _CTX
        )
        (posted,) = gh.posted_reviews()
        assert len(posted["body"]) <= 60_000
        assert "more finding(s) omitted" in posted["body"]
        assert posted["body"].endswith(github_api._REVIEW_FOOTER)
        assert "Summary text." in posted["body"]
        assert result["inline_count"] == 1

    async def test_inline_comment_bodies_are_capped(self, gh):
        await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "big", 11, body="y" * 20_000)]), _CTX
        )
        (posted,) = gh.posted_reviews()
        assert len(posted["comments"][0]["body"]) <= 8_000

    async def test_a_non_anchor_422_is_not_folded(self, gh):
        gh.review_responses = [_422("Body is too long (maximum is 65536 characters)")]
        result = await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "In diff", 11)]), _CTX
        )
        assert "error" in result
        assert "too long" in result["error"]
        assert len(gh.posted_reviews()) == 1


class TestCommentIdsAndRace:
    async def test_comment_ids_follow_pagination(self, gh):
        gh.comment_pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        result = await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "In diff", 11)]), _CTX
        )
        assert result["comment_ids"] == [1, 2, 3]

    async def test_head_moving_while_reading_files_aborts(self, gh):
        gh.pr_after_first_read = _pr(head="d" * 40)
        result = await github_api._github_create_review(
            _review_args("COMMENT", [_issue("blocker", "In diff", 11)]), _CTX
        )
        assert "head moved" in result["error"]
        assert result["head_sha"] == "d" * 40
        assert gh.posted_reviews() == []


class TestCompareHardening:
    async def test_flags_report_truncation(self, gh):
        gh.compare = {
            "status": "ahead",
            "total_commits": 400,
            "commits": [{"sha": "s"}] * 250,
            "files": [{"filename": f"f{i}"} for i in range(300)],
        }
        result = await github_api._github_compare({"repo": _REPO, "base": "a", "head": "b"}, _CTX)
        assert result["files_truncated"] is True
        assert result["commits_truncated"] is True

    async def test_flags_false_when_complete(self, gh):
        gh.compare = {"status": "ahead", "total_commits": 1, "commits": [{"sha": "s"}], "files": []}
        result = await github_api._github_compare({"repo": _REPO, "base": "a", "head": "b"}, _CTX)
        assert result["files_truncated"] is False
        assert result["commits_truncated"] is False

    async def test_404_with_unreachable_repo_is_an_error_not_missing(self, gh):
        gh.repo_status = 404
        result = await github_api._github_compare({"repo": _REPO, "base": "a", "head": "b"}, _CTX)
        assert "status" not in result
        assert _REPO in result["error"]

    async def test_404_with_unreachable_head_is_an_error(self, gh):
        gh.commit_status = 404
        result = await github_api._github_compare({"repo": _REPO, "base": "a", "head": "b"}, _CTX)
        assert "status" not in result
        assert "head" in result["error"]
