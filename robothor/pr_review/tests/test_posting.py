"""The pure half of review posting: anchors, severities, the body, the SHA rule.

GitHub rejects a WHOLE review when a single inline comment points at a line
outside the diff, so the partition is what decides whether a review lands at
all. Everything here is pure; the HTTP half lives in
``robothor/engine/tools/handlers/github_api.py``.
"""

from __future__ import annotations

from robothor.pr_review.posting import (
    commentable_lines,
    compose_body,
    decide_review,
    format_issue_comment,
    own_pr_body,
    partition_comments,
    split_by_severity,
)

PATCH = "\n".join(
    [
        "@@ -10,4 +10,5 @@ def a():",
        " ctx10",
        "-old11",
        "+new11",
        "+new12",
        " ctx12",
        "@@ -40,2 +41,2 @@",
        " ctx40",
        "-old41",
        "+new42",
        "\\ No newline at end of file",
    ]
)

FILES = {"src/a.py": PATCH, "logo.png": None}


def _issue(severity: str, title: str, line: int | None = 1, **kw):
    base = {
        "path": "src/a.py",
        "line": line,
        "start_line": None,
        "side": "RIGHT",
        "severity": severity,
        "title": title,
        "body": f"{title} body",
    }
    base.update(kw)
    return base


# ─── commentable_lines ─────────────────────────────────────────────


class TestCommentableLines:
    def test_maps_both_sides_with_hunk_ids(self):
        lines = commentable_lines(PATCH)
        assert sorted(lines["RIGHT"]) == [10, 11, 12, 13, 41, 42]
        assert sorted(lines["LEFT"]) == [10, 11, 12, 40, 41]
        assert lines["RIGHT"][11] == 0
        assert lines["RIGHT"][42] == 1

    def test_no_newline_marker_consumes_no_line(self):
        lines = commentable_lines(PATCH)
        assert 43 not in lines["RIGHT"]
        assert 42 not in lines["LEFT"]

    def test_missing_patch_is_empty(self):
        assert commentable_lines(None) == {"LEFT": {}, "RIGHT": {}}
        assert commentable_lines("") == {"LEFT": {}, "RIGHT": {}}

    def test_text_before_first_hunk_is_ignored(self):
        lines = commentable_lines("diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n+y")
        assert lines["RIGHT"] == {1: 0}


# ─── partition_comments ────────────────────────────────────────────


class TestPartitionComments:
    def test_in_diff_stays_inline_and_everything_else_goes_to_the_body(self):
        comments = [
            {"path": "src/a.py", "line": 11, "side": "RIGHT", "body": "ok"},
            {"path": "src/a.py", "line": 30, "side": "RIGHT", "body": "outside diff"},
            {"path": "src/other.py", "line": 1, "side": "RIGHT", "body": "file not in PR"},
            {"path": "logo.png", "line": 1, "side": "RIGHT", "body": "binary"},
            {"path": "", "line": None, "body": "PR-wide"},
        ]
        inline, body = partition_comments(comments, FILES)
        assert [(c["path"], c["line"], c["side"]) for c in inline] == [("src/a.py", 11, "RIGHT")]
        assert [c["body"] for c in body] == [
            "outside diff",
            "file not in PR",
            "binary",
            "PR-wide",
        ]

    def test_left_side_anchors_on_removed_lines(self):
        comments = [
            {"path": "src/a.py", "line": 11, "side": "LEFT", "body": "removed line"},
            # 13 exists on the RIGHT only: a LEFT anchor there is out of the diff.
            {"path": "src/a.py", "line": 13, "side": "LEFT", "body": "bad left"},
        ]
        inline, body = partition_comments(comments, FILES)
        assert [(c["line"], c["side"]) for c in inline] == [(11, "LEFT")]
        assert [c["body"] for c in body] == ["bad left"]

    def test_missing_side_defaults_to_right(self):
        inline, _ = partition_comments([{"path": "src/a.py", "line": 12, "body": "x"}], FILES)
        assert inline[0]["side"] == "RIGHT"

    def test_multi_line_range_kept_only_within_one_hunk(self):
        comments = [
            {"path": "src/a.py", "line": 13, "start_line": 11, "side": "RIGHT", "body": "range"},
            {"path": "src/a.py", "line": 42, "start_line": 12, "side": "RIGHT", "body": "x-hunk"},
            {"path": "src/a.py", "line": 12, "start_line": 12, "side": "RIGHT", "body": "same"},
        ]
        inline, body = partition_comments(comments, FILES)
        assert body == []
        assert inline[0]["start_line"] == 11
        assert inline[0]["start_side"] == "RIGHT"
        # A cross-hunk range degrades to a single-line anchor, it is not dropped.
        assert "start_line" not in inline[1]
        assert inline[1]["line"] == 42
        # start_line must precede line, or GitHub 422s.
        assert "start_line" not in inline[2]

    def test_original_dicts_come_back_in_the_body_list(self):
        issue = _issue("major", "Outside", 99)
        _, body = partition_comments([issue], FILES)
        assert body[0] is issue

    def test_non_integer_line_goes_to_the_body(self):
        _, body = partition_comments([{"path": "src/a.py", "line": "11", "body": "x"}], FILES)
        assert len(body) == 1


# ─── split_by_severity ─────────────────────────────────────────────


class TestSplitBySeverity:
    def test_blockers_and_majors_are_inline_eligible(self):
        issues = [
            _issue("blocker", "b"),
            _issue("Major", "M"),
            _issue("minor", "m"),
            _issue("nit", "n"),
            _issue("moderate", "x"),
        ]
        blocking, non_blocking = split_by_severity(issues)
        assert [i["title"] for i in blocking] == ["b", "M"]
        assert [i["title"] for i in non_blocking] == ["m", "n", "x"]

    def test_custom_inline_severities(self):
        blocking, _ = split_by_severity([_issue("minor", "m")], inline_severities={"minor"})
        assert len(blocking) == 1

    def test_missing_severity_is_non_blocking(self):
        _, non_blocking = split_by_severity([{"title": "x", "body": "y"}])
        assert len(non_blocking) == 1


# ─── compose_body ──────────────────────────────────────────────────


class TestComposeBody:
    def test_non_blocking_findings_have_their_own_heading(self):
        body = compose_body(
            "Looks good.", [], [_issue("minor", "Rename", 12), _issue("nit", "Typo", None)]
        )
        assert body.startswith("Looks good.")
        assert "### Non-blocking (for awareness)" in body
        assert "`src/a.py:12` — **minor: Rename**" in body
        assert "**nit: Typo**" in body
        assert "### Other findings" not in body

    def test_out_of_diff_blocking_findings_are_listed_separately(self):
        body = compose_body("", [_issue("major", "Outside diff", 99)], [])
        assert "### Other findings" in body
        assert "`src/a.py:99` — **major: Outside diff**" in body
        assert "Non-blocking" not in body

    def test_prior_issues_are_reported_with_their_status(self):
        prior = [
            {"description": "Null check", "status": "resolved", "note": "Guard added"},
            {"description": "SQL injection", "status": "unresolved", "note": "Still concatenated"},
        ]
        body = compose_body("Re-review.", [], [], prior_issues=prior)
        assert "### Previous findings" in body
        assert "**resolved** — Null check: Guard added" in body
        assert "**unresolved** — SQL injection: Still concatenated" in body

    def test_multiline_issue_bodies_stay_inside_the_list_item(self):
        body = compose_body("", [_issue("major", "T", 1, body="line one\nline two")], [])
        assert "  line one\n  line two" in body

    def test_clean_review_with_nothing_to_say_is_empty(self):
        assert compose_body("", [], []) == ""
        assert compose_body("   ", [], [], prior_issues=[]) == ""

    def test_footer_is_appended_when_there_is_content(self):
        body = compose_body("Hi", [], [], footer="<sub>Automated review</sub>")
        assert body.endswith("<sub>Automated review</sub>")
        assert compose_body("", [], [], footer="<sub>x</sub>") == ""


class TestFormatting:
    def test_issue_comment_format(self):
        assert format_issue_comment(_issue("blocker", "Leak")) == "**blocker: Leak**\n\nLeak body"

    def test_own_pr_body_prefixes(self):
        assert own_pr_body("APPROVE", "body") == "APPROVED\n\nbody"
        assert own_pr_body("APPROVE", "") == "APPROVED"
        assert own_pr_body("REQUEST_CHANGES", "body") == "CHANGES REQUESTED\n\nbody"
        assert own_pr_body("COMMENT", "body") == "body"


# ─── decide_review ─────────────────────────────────────────────────


class TestDecideReview:
    def test_full_initial_review_for_a_new_pr(self):
        d = decide_review(kind="initial", head_sha="a", last_reviewed_sha=None)
        assert (d.action, d.mode) == ("review", "full")

    def test_never_reviews_the_same_head_twice(self):
        d = decide_review(
            kind="initial", head_sha="a", last_reviewed_sha=None, completed_review_at_head=True
        )
        assert (d.action, d.reason) == ("skip", "already_reviewed")
        d = decide_review(kind="initial", head_sha="a", last_reviewed_sha="a")
        assert d.action == "skip"

    def test_rereview_at_same_sha_reports_no_new_commits(self):
        d = decide_review(kind="rereview", head_sha="a", last_reviewed_sha="a")
        assert (d.action, d.reason) == ("skip", "no_new_commits")

    def test_rereview_is_incremental_when_the_head_moved_forward(self):
        d = decide_review(
            kind="rereview", head_sha="b", last_reviewed_sha="a", compare_status="ahead"
        )
        assert (d.action, d.mode, d.since_sha) == ("review", "incremental", "a")

    def test_rereview_without_compare_status_is_incremental(self):
        d = decide_review(kind="rereview", head_sha="b", last_reviewed_sha="a")
        assert d.mode == "incremental"

    def test_rereview_without_a_previous_sha_is_full(self):
        d = decide_review(kind="rereview", head_sha="b", last_reviewed_sha=None)
        assert (d.action, d.mode) == ("review", "full")

    def test_force_push_back_to_a_reviewed_sha_is_skipped(self):
        d = decide_review(
            kind="rereview", head_sha="a", last_reviewed_sha="b", completed_review_at_head=True
        )
        assert (d.action, d.reason) == ("skip", "no_new_commits")

    def test_diverged_behind_or_missing_history_forces_a_full_review(self):
        for status in ("diverged", "behind", "missing"):
            d = decide_review(
                kind="rereview", head_sha="b", last_reviewed_sha="a", compare_status=status
            )
            assert (d.action, d.mode, d.since_sha) == ("review", "full", None), status

    def test_identical_compare_is_no_new_commits(self):
        d = decide_review(
            kind="rereview", head_sha="b", last_reviewed_sha="a", compare_status="identical"
        )
        assert (d.action, d.reason) == ("skip", "no_new_commits")
