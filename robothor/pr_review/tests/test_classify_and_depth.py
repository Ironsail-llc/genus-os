"""PR-link detection, the re-review keyword classifier, and review depth."""

from __future__ import annotations

import pytest

from robothor.pr_review.classify import classify_rereview, extract_pr_refs, match_allowed_prs
from robothor.pr_review.depth import Depth, DepthPolicy


def test_extract_pr_refs_dedupes_and_keeps_order():
    text = (
        "please look https://github.com/acme/widgets/pull/12 and "
        "<https://github.com/acme/gadgets/pull/3|this> and again "
        "https://github.com/Acme/Widgets/pull/12/files"
    )
    refs = extract_pr_refs(text)
    assert [(r.owner, r.repo, r.number) for r in refs] == [
        ("acme", "widgets", 12),
        ("acme", "gadgets", 3),
    ]


def test_extract_ignores_non_pr_links():
    assert extract_pr_refs("https://github.com/acme/widgets/issues/4") == []
    assert extract_pr_refs("https://github.com/acme/widgets/pull/12abc") == []


def test_match_allowed_prs_uses_the_configured_casing():
    found = match_allowed_prs("https://github.com/ACME/widgets/pull/9", ["acme/Widgets"])
    assert [(p.repo_full, p.number, p.url) for p in found] == [
        ("acme/Widgets", 9, "https://github.com/acme/Widgets/pull/9")
    ]


def test_match_allowed_prs_drops_other_repos():
    assert match_allowed_prs("https://github.com/other/repo/pull/1", ["acme/widgets"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "ptal",
        "re-review please",
        "fixed, can you look again",
        "Pushed the changes",
        "rereview",
        "@Alice Updated PR. Can you re-review?",
        "can you take another look?",
    ],
)
def test_rereview_requests(text):
    assert classify_rereview(text) == "rereview"


@pytest.mark.parametrize(
    "text",
    [
        "thanks",
        "ok",
        "👍",
        "LGTM",
        "",
        "  ",
        "#12: Approved",
        "#12: Comments/change request",
        "#12: No changes?",
        "#747: This PR is merged; skipping the review.",
        "both approved with comments",
        "Changes requested on both",
    ],
)
def test_acknowledgements_are_other(text):
    assert classify_rereview(text) == "other"


@pytest.mark.parametrize(
    "text",
    [
        "not fixed yet",
        "will fix tomorrow",
        "is this fixed?",
        "bot will you review?",
        "working on it, haven't pushed",
        "why does the parser need this branch",
    ],
)
def test_negated_or_unclear_replies_are_ambiguous(text):
    assert classify_rereview(text) == "ambiguous"


def test_depth_skips_bot_authors_and_lockfile_only_changes():
    policy = DepthPolicy()
    assert policy.depth_for(author="dependabot[bot]", changed_lines=5) is Depth.SKIP
    assert (
        policy.depth_for(files=("package-lock.json", "yarn.lock"), changed_lines=900) is Depth.SKIP
    )


def test_depth_small_is_light_and_risky_is_full():
    policy = DepthPolicy()
    assert policy.depth_for(changed_lines=20, files=("src/a.py",)) is Depth.LIGHT
    assert policy.depth_for(labels=("security",), changed_lines=5, files=("a.py",)) is Depth.FULL
    assert policy.depth_for(changed_lines=400, files=("src/a.py",)) is Depth.FULL


def test_depth_from_github_files():
    files = [
        {"filename": "src/a.py", "additions": 10, "deletions": 2},
        {"filename": "dist/app.min.js", "additions": 5000, "deletions": 0},
    ]
    depth = DepthPolicy().depth_for_pr(pr={"user": {"login": "alice"}, "labels": []}, files=files)
    assert depth is Depth.FULL  # 5012 changed lines in total; one real file


def test_snap_matches_the_suffix_only():
    policy = DepthPolicy()
    assert policy.is_generated("tests/__snapshots__/a.test.ts.snap")
    assert not policy.is_generated("src/snapshot.snapshot.ts")
    assert not policy.is_generated("src/snapper.ts")
    assert not policy.is_generated("docs/uv.lock.md")
    assert policy.is_generated("web/package-lock.json")
    assert policy.is_generated("api/client.generated.ts")


def test_labels_skip_only_when_configured():
    assert DepthPolicy().depth_for(labels=("dependencies",), changed_lines=5, files=("a.py",)) is (
        Depth.LIGHT
    )
    policy = DepthPolicy(skip_labels=("no-review",))
    assert policy.depth_for(labels=("No-Review",), files=("a.py",)) is Depth.SKIP
