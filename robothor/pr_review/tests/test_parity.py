"""Parity with an existing review bot: guidelines file, ticket criteria, model knobs, Chat wording.

Every outside system is faked. See docs/PR_REVIEWER.md, "Parity with an
existing review bot".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from robothor.pr_review.config import ReviewerConfig, parse_repo_specs
from robothor.pr_review.intake import Intake
from robothor.pr_review.prompt import (
    Guidelines,
    ReviewContext,
    TicketContext,
    build_review_prompt,
    load_guidelines,
)
from robothor.pr_review.review import finalize, prepare
from robothor.pr_review.store import MemoryStore
from robothor.pr_review.tests.fakes import REPO, FakeChat, FakeGitHub, FakeTasks, make_pr
from robothor.pr_review.tests.test_review import (
    SHA1,
    TENANT,
    FakeJob,
    StartRecorder,
    _issue,
    _output,
    _queue,
    make_env,
)

URL = f"https://github.com/{REPO}/pull/7"
OTHER = "acme/gadgets"


# ── 1. configuration ─────────────────────────────────────────────────


def test_repos_accept_the_owner_repo_prefix_format():
    repos, per_repo, global_prefixes = parse_repo_specs(f"{REPO}:abc, {OTHER}:XYZ", "")
    assert repos == (REPO, OTHER)
    assert per_repo == ((REPO, "ABC"), (OTHER, "XYZ"))
    assert global_prefixes == ()


def test_ticket_prefixes_accept_per_repo_and_bare_entries():
    repos, per_repo, global_prefixes = parse_repo_specs(REPO, f"{OTHER}:XYZ,def")
    assert repos == (REPO,)
    cfg = ReviewerConfig(
        repos=repos, repo_ticket_prefixes=per_repo, ticket_prefixes=global_prefixes
    )
    assert cfg.prefixes_for("ACME/Gadgets") == ("XYZ",)
    assert cfg.prefixes_for(REPO) == ("DEF",)


def test_settings_defaults_match_the_review_bot_profile(monkeypatch):
    from robothor.pr_review.config import load_config
    from robothor.settings import reset_settings

    for name in (
        "ROBOTHOR_PR_REVIEW_EFFORT",
        "ROBOTHOR_PR_REVIEW_MAX_TURNS",
        "ROBOTHOR_PR_REVIEW_ROUND_TIMEOUT",
        "ROBOTHOR_PR_REVIEW_BUDGET_USD",
        "ROBOTHOR_PR_REVIEW_APPROVED_REACTION",
        "ROBOTHOR_PR_REVIEW_GUIDELINES_PATH",
        "ROBOTHOR_PR_REVIEW_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_PR_REVIEW_REPOS", f"{REPO}:ABC")
    reset_settings()
    try:
        cfg = load_config()
    finally:
        monkeypatch.undo()
        reset_settings()
    assert cfg.repos == (REPO,)
    assert cfg.prefixes_for(REPO) == ("ABC",)
    assert cfg.review_effort == "high"
    assert cfg.review_max_turns == 80
    assert cfg.review_round_timeout_s == 1800
    assert cfg.review_budget_usd == 25
    assert cfg.approved_reaction == "\U0001f44d"
    assert cfg.claim_reaction == "\U0001f440"
    assert cfg.guidelines_path == ""
    assert cfg.review_model == "opus"  # the alias: always the newest Opus


# ── 2. guidelines and prompt ─────────────────────────────────────────


def _ctx(**kw: Any) -> ReviewContext:
    base: dict[str, Any] = {
        "repo": REPO,
        "number": 7,
        "url": URL,
        "title": "ABC-12 cart badge",
        "author": "alice",
        "head_ref": "feat/abc-12",
        "base_ref": "main",
        "head_sha": SHA1,
    }
    base.update(kw)
    return ReviewContext(**base)


def test_an_instance_guidelines_file_replaces_the_skill(tmp_path):
    path = tmp_path / "pr-review-guidelines.md"
    path.write_text("# Team guidelines\nCheck the money paths first.")
    g = load_guidelines(str(path), "GENERIC SKILL BODY")
    assert g.source == "instance" and g.path == str(path)
    prompt = build_review_prompt(g, _ctx())
    assert "Check the money paths first." in prompt
    assert "GENERIC SKILL BODY" not in prompt
    assert f'<review_guidelines path="{path}" sha256="{g.sha256[:12]}">' in prompt
    assert "the guidelines win" in prompt
    # Genus's operating contract stays: real tools, the output fields, the verdict rule.
    assert "Read, Grep and Glob" in prompt
    assert "prior_issues (comment_id, description, status, note)" in prompt
    assert "never approved" in prompt
    assert "git diff origin/main...HEAD" in prompt


def test_a_missing_or_empty_guidelines_file_falls_back_to_the_skill(tmp_path):
    assert load_guidelines(str(tmp_path / "nope.md"), "SKILL").source == "skill"
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    assert load_guidelines(str(empty), "SKILL").text == "SKILL"
    assert load_guidelines("", "SKILL").source == "skill"


def test_the_prompt_never_promises_tools_a_review_job_lacks():
    prompt = build_review_prompt(Guidelines(text="g"), _ctx())
    assert "There is no network: no gh" in prompt
    assert "`gh pr view`" not in prompt


def test_a_fetched_ticket_and_its_criteria_are_in_the_prompt():
    ticket = TicketContext(
        key="ABC-12",
        state="fetched",
        url="https://jira.example.com/browse/ABC-12",
        summary="Cart badge",
        status="In Review",
        description="Show the count.",
        acceptance_criteria="- Badge shows 0-99\n- 99+ above that",
    )
    prompt = build_review_prompt("g", _ctx(ticket_key="ABC-12", ticket=ticket))
    assert '<ticket key="ABC-12">' in prompt
    assert "- 99+ above that" in prompt
    assert "Status: In Review" in prompt
    assert "meet each acceptance criterion" in prompt


@pytest.mark.parametrize(
    ("state", "phrase"),
    [("not_configured", "no ticket system is configured"), ("unavailable", "could not be fetched")],
)
def test_an_unreadable_ticket_is_said_plainly(state, phrase):
    ticket = TicketContext(key="ABC-12", state=state, error="HTTP 503")
    prompt = build_review_prompt("g", _ctx(ticket_key="ABC-12", ticket=ticket))
    assert phrase in prompt
    assert "Do not guess or invent its acceptance criteria" in prompt


def test_no_ticket_is_said_plainly():
    assert "Linked ticket: none found" in build_review_prompt("g", _ctx())


def test_rereview_prompt_carries_previous_issues_and_the_compare_range():
    prompt = build_review_prompt(
        "g",
        _ctx(
            mode="incremental",
            since_sha="0" * 40,
            previous_verdict="REQUEST_CHANGES",
            previous_summary="Needs a guard.",
            previous_issues=[{"comment_id": 77, "severity": "major", "title": "guard"}],
        ),
    )
    assert "This is a RE-REVIEW" in prompt
    assert '"comment_id": 77' in prompt
    assert f"Incremental diff 0000000...{SHA1[:7]}" in prompt
    assert f"git diff {'0' * 40}..HEAD" in prompt
    assert "Previous verdict: REQUEST_CHANGES" in prompt


# ── prepare: guidelines file, ticket fetch, model knobs ──────────────


async def _prepare(env, cfg, tmp_path, **kw):
    async def checkout(dest, **_kw):
        return tmp_path

    async def reader(*_a):
        return None

    start = StartRecorder()
    result = await prepare(
        cfg,
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="GENERIC SKILL BODY",
        token="",
        checkout=checkout,
        reader=reader,
        start_job=start,
        **kw,
    )
    return result, start


async def test_prepare_uses_the_guidelines_file_and_the_model_knobs(tmp_path):
    env = await make_env()
    await _queue(env)
    guide = tmp_path / "g.md"
    guide.write_text("TEAM RULES")
    cfg = ReviewerConfig(repos=(REPO,), guidelines_path=str(guide))
    result, start = await _prepare(env, cfg, tmp_path)
    [args] = start.calls
    assert "TEAM RULES" in args["task"] and "GENERIC SKILL BODY" not in args["task"]
    assert args["effort"] == "high"
    assert args["max_turns"] == 80
    assert args["round_timeout_s"] == 1800
    assert args["max_budget_usd"] == 25
    assert args["model"] == "opus"  # the alias, never a pinned id
    assert result["guidelines"] == "instance"


async def test_prepare_fetches_the_ticket_and_redacts_it(tmp_path):
    env = await make_env()
    await _queue(env)
    env["github"].prs[(REPO, 7)]["title"] = "abc-12: cart badge"
    secret = "ghp_" + "A1b2C3d4" * 5
    asked: list[str] = []

    async def fetch(key: str) -> dict[str, Any]:
        asked.append(key)
        return {
            "key": key,
            "summary": "Cart badge",
            "status": "In Review",
            "description": f"token {secret} " + "x" * 20_000,
            "acceptance_criteria": "- Badge shows 0-99",
            "url": "https://jira.example.com/browse/ABC-12",
        }

    cfg = ReviewerConfig(repos=(REPO,), repo_ticket_prefixes=((REPO, "ABC"),))
    result, start = await _prepare(env, cfg, tmp_path, fetch_ticket=fetch)
    task = start.calls[0]["task"]
    assert asked == ["ABC-12"] and result["ticket"] == "ABC-12"
    assert "- Badge shows 0-99" in task
    assert secret not in task
    assert "cut for length" in task
    ticket_block = task.split('<ticket key="ABC-12">', 1)[1].split("</ticket>", 1)[0]
    assert len(ticket_block) < 8_500


async def test_prepare_finds_a_ticket_in_a_commit_trailer(tmp_path):
    env = await make_env()
    await _queue(env)
    env["github"].commits[(REPO, 7)] = [{"commit": {"message": "fix\n\nRefs: XYZ-9"}}]
    cfg = ReviewerConfig(repos=(REPO,), repo_ticket_prefixes=((REPO, "XYZ"),))

    async def not_configured(key: str) -> None:
        return None

    result, start = await _prepare(env, cfg, tmp_path, fetch_ticket=not_configured)
    assert result["ticket"] == "XYZ-9"
    assert "no ticket system is configured" in start.calls[0]["task"]


async def test_prepare_reports_a_failed_ticket_fetch(tmp_path):
    env = await make_env()
    await _queue(env)
    env["github"].prs[(REPO, 7)]["title"] = "ABC-12 badge"

    async def broken(key: str) -> dict[str, Any]:
        return {"error": "JIRA API error: 503"}

    cfg = ReviewerConfig(repos=(REPO,), ticket_prefixes=("ABC",))
    _, start = await _prepare(env, cfg, tmp_path, fetch_ticket=broken)
    assert "could not be fetched (JIRA API error: 503)" in start.calls[0]["task"]


# ── 4. Chat wording and reactions ────────────────────────────────────


async def _finalize(env, output, cfg=None):
    return await finalize(
        cfg or ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        job=FakeJob(result={"structured_output": output}),
        github=env["github"],
        poster=env["poster"],
        chat=env["chat"],
    )


async def test_approval_replies_approved_and_swaps_the_claim_reaction():
    env = await make_env()
    await _finalize(env, _output("APPROVE", [_issue("nit")]))
    assert env["chat"].replies[-1][2] == f"<{URL}|#7>: Approved"
    assert ("spaces/AAAA/messages/m1", "\U0001f44d") in env["chat"].reactions
    assert env["chat"].unreactions == [("spaces/AAAA/messages/m1", "\U0001f440")]


async def test_a_blocking_review_says_comments_change_request_with_the_count():
    env = await make_env()
    await _finalize(env, _output("APPROVE", [_issue("blocker"), _issue("major", 4)]))
    assert env["chat"].replies[-1][2] == f"<{URL}|#7>: Comments/change request — 2 blocking"
    assert env["chat"].unreactions == []


async def test_a_non_blocking_comment_says_comments_change_request():
    env = await make_env()
    await _finalize(env, _output("COMMENT", [_issue("minor")]))
    assert env["chat"].replies[-1][2] == f"<{URL}|#7>: Comments/change request"


async def test_a_failure_says_how_many_attempts_and_how_to_retry():
    env = await make_env()
    await _finalize(env, {"verdict": "nope"})
    text = env["chat"].replies[-1][2]
    assert text.startswith(f"<{URL}|#7>: ⚠️ Automated review failed after 1 attempt: ")
    assert text.endswith('\nReply "re-review" to try again.')


# ── intake wording ───────────────────────────────────────────────────


def _intake(store, github, chat, tasks, cfg=None):
    return Intake(
        cfg or ReviewerConfig(repos=(REPO,), chat_space="spaces/AAAA"),
        store,
        TENANT,
        tasks=tasks,
        github=github,
        chat=chat,
        now=datetime(2026, 10, 5, 10, 30, tzinfo=UTC),
    )


async def _tracked(status: str = "changes_requested", last: str = SHA1):
    from robothor.pr_review.store import PrReviewRow

    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, SHA1))
    await store.save(
        PrReviewRow(
            tenant_id=TENANT,
            repo=REPO,
            number=7,
            url=URL,
            status=status,
            last_reviewed_sha=last,
            queued_sha=SHA1,
            chat_space="spaces/AAAA",
            chat_thread="spaces/AAAA/threads/t1",
            chat_message="spaces/AAAA/messages/m0",
            chat_poster="users/alice",
            last_review={"verdict": "REQUEST_CHANGES", "url": f"{URL}#pullrequestreview-1"},
        )
    )
    return store, github, chat, tasks


async def test_a_rereview_with_no_new_commits_says_no_changes():
    store, github, chat, tasks = await _tracked()
    chat.post("ptal", reply=True)
    await _intake(store, github, chat, tasks).run()
    assert chat.replies[-1][2] == f"<{URL}|#7>: No changes?"


async def test_a_rereview_during_a_review_says_one_is_running():
    store, github, chat, tasks = await _tracked(status="reviewing", last="")
    chat.post("fixed, ptal", reply=True)
    await _intake(store, github, chat, tasks).run()
    assert chat.replies[-1][2] == (
        f"<{URL}|#7>: A review is already running for this PR. "
        "I'll check for newer commits once it's posted."
    )


async def test_a_request_answered_with_in_progress_is_not_also_told_no_changes():
    """Told "I'll check for newer commits once it's posted", finding none is not news."""
    store, github, chat, tasks = await _tracked(status="reviewing", last="")
    chat.post("fixed, ptal", reply=True)
    await _intake(store, github, chat, tasks).run()
    # The running review is posted for the same head...
    env = {"store": store, "github": github, "chat": chat, "poster": None}
    from robothor.pr_review.tests.test_review import FakePoster

    env["poster"] = FakePoster()
    row = await store.get(TENANT, REPO, 7)
    row.job_id = "job-1"
    await store.save(row)
    await _finalize(env, _output("COMMENT", []))
    assert (await store.get(TENANT, REPO, 7)).followup is True
    replies = len(chat.replies)
    # ...and the follow-up finds no new commits: silence, like the old bot.
    await _intake(store, github, chat, tasks).run(poll=False)
    assert len(chat.replies) == replies
    assert tasks.created == []


async def test_a_merged_pr_says_it_is_skipped():
    store, github, chat, tasks = await _tracked(status="pending", last="")
    pr = github.prs[(REPO, 7)]
    pr["state"], pr["merged"] = "closed", True
    row = await store.get(TENANT, REPO, 7)
    row.pending_trigger = "initial"
    await store.save(row)
    await _intake(store, github, chat, tasks).run(poll=False)
    assert chat.replies[-1][2] == f"<{URL}|#7>: This PR is merged; skipping the review."


# ── 5. on demand: pr_review_intake(pr=...) ───────────────────────────


async def test_an_on_demand_request_queues_one_pr():
    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, SHA1))
    out = await _intake(store, github, chat, tasks).request(URL)
    assert out["requested"]["status"] == "queued"
    assert out["requested"]["pr"] == f"{REPO}#7"
    assert len(tasks.created) == 1
    row = await store.get(TENANT, REPO, 7)
    assert row.source == "operator" and not row.chat_thread
    assert chat.replies == [] and chat.reactions == []


async def test_an_on_demand_request_accepts_owner_repo_number_and_reports_a_done_review():
    store, github, chat, tasks = await _tracked(status="approved")
    out = await _intake(store, github, chat, tasks).request(f"{REPO}#7")
    assert out["requested"]["status"] == "approved"
    assert out["requested"]["review_url"] == f"{URL}#pullrequestreview-1"
    assert "no new commits" in out["requested"]["note"]
    assert tasks.created == []


async def test_an_on_demand_request_for_a_threadless_row_reports_via_the_digest():
    from robothor.pr_review.store import PrReviewRow

    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, SHA1))
    await store.save(PrReviewRow(tenant_id=TENANT, repo=REPO, number=7, url=URL, source="github"))
    await _intake(store, github, chat, tasks).request(URL)
    assert (await store.get(TENANT, REPO, 7)).source == "operator"


async def test_an_on_demand_request_refuses_other_repositories():
    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    out = await _intake(store, github, chat, tasks).request("https://github.com/evil/x/pull/1")
    assert "not in ROBOTHOR_PR_REVIEW_REPOS" in out["error"]
    out = await _intake(store, github, chat, tasks).request("not a pr")
    assert "error" in out
    assert tasks.created == []


async def test_an_operator_review_digest_is_returned_without_a_chat_thread():
    env = await make_env()
    row = await env["store"].get(TENANT, REPO, 7)
    row.chat_space = row.chat_thread = row.chat_message = ""
    row.source = "operator"
    await env["store"].save(row)
    result = await _finalize(env, _output("APPROVE", []))
    assert result["digest"].startswith(f"PR review {REPO}#7: Approved")
    assert "digest line, verbatim" in result["next"]


def test_main_can_ask_for_a_review_and_is_told_how():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3] / "templates" / "agents" / "core" / "main"
    manifest = yaml.safe_load(
        (root / "manifest.template.yaml").read_text().replace("{{ timezone }}", "UTC")
    )
    assert "pr_review_intake" in manifest["tools_opt_in"]
    # Main asks; it must not be able to prepare or post a review itself.
    assert not {"pr_review_prepare", "pr_review_finalize"} & set(manifest["tools_opt_in"])
    instructions = (root / "instructions.template.md").read_text()
    assert 'pr_review_intake(pr="' in instructions
    assert "requested.review_url" in instructions
