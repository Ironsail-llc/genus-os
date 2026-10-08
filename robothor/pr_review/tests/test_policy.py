"""The verdict is recomputed in code; the model's word never posts unchecked."""

from __future__ import annotations

import pytest

from robothor.pr_review.policy import (
    NO_TICKET_TITLE,
    decide_verdict,
    extract_ticket_key,
    guard_posted_verdict,
)


def _issue(severity: str, title: str = "t") -> dict:
    return {"path": "src/a.py", "line": 3, "severity": severity, "title": title, "body": "b"}


def test_model_approve_with_a_blocker_becomes_request_changes():
    result = decide_verdict("APPROVE", [_issue("blocker")])
    assert result.verdict == "REQUEST_CHANGES"
    assert result.overridden is True
    assert result.model_verdict == "APPROVE"
    assert len(result.blocking) == 1


def test_model_approve_with_a_major_is_never_approved():
    assert decide_verdict("APPROVE", [_issue("major")]).verdict == "REQUEST_CHANGES"


def test_blocking_event_can_be_comment_instead():
    result = decide_verdict("APPROVE", [_issue("blocker")], blocking_event="COMMENT")
    assert result.verdict == "COMMENT"


def test_an_invalid_blocking_event_falls_back_to_request_changes():
    result = decide_verdict("APPROVE", [_issue("blocker")], blocking_event="APPROVE")
    assert result.verdict == "REQUEST_CHANGES"


def test_minor_and_nit_never_block_an_approval():
    result = decide_verdict("APPROVE", [_issue("minor"), _issue("nit")])
    assert result.verdict == "APPROVE"
    assert result.overridden is False
    assert result.blocking == []


def test_nothing_blocking_approves_whatever_the_model_proposed():
    # Team feedback 2026-10-08: "pass on minors". Minor issues and nits stay
    # listed as suggestions, but they never hold back the approval.
    for proposed in ("COMMENT", "REQUEST_CHANGES", "LGTM", ""):
        result = decide_verdict(proposed, [_issue("minor"), _issue("nit")])
        assert result.verdict == "APPROVE", proposed
        assert result.blocking == []


def test_an_approval_the_model_did_not_give_is_recorded_as_overridden():
    result = decide_verdict("REQUEST_CHANGES", [_issue("minor")])
    assert result.overridden is True and result.model_verdict == "REQUEST_CHANGES"


def test_blocking_issue_upgrades_a_comment_verdict():
    assert decide_verdict("COMMENT", [_issue("major")]).verdict == "REQUEST_CHANGES"


def test_unknown_model_verdict_with_a_blocker_blocks():
    assert decide_verdict("LGTM", [_issue("blocker")]).verdict == "REQUEST_CHANGES"


def test_a_prior_blocker_the_author_justified_is_accepted():
    # Team feedback 2026-10-08: the author answered the finding ("out of
    # scope", "not needed") and the reviewer agreed — it clears like a fix.
    prior = [{"comment_id": 11, "description": "x", "status": "accepted", "note": "out of scope"}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "major"})
    assert result.verdict == "APPROVE"
    assert result.resolved_ids == [11]


def test_an_unresolved_prior_blocker_still_blocks():
    prior = [{"comment_id": 11, "description": "x", "status": "unresolved", "note": ""}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "blocker"})
    assert result.verdict == "REQUEST_CHANGES"


def test_a_partially_resolved_prior_major_still_blocks():
    prior = [{"comment_id": 11, "description": "x", "status": "partially_resolved", "note": ""}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "major"})
    assert result.verdict == "REQUEST_CHANGES"


def test_a_resolved_prior_blocker_allows_approval():
    prior = [{"comment_id": 11, "description": "x", "status": "resolved", "note": ""}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "blocker"})
    assert result.verdict == "APPROVE"


def test_a_prior_minor_left_unresolved_does_not_block():
    prior = [{"comment_id": 11, "description": "x", "status": "unresolved", "note": ""}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "minor"})
    assert result.verdict == "APPROVE"


def test_missing_ticket_adds_a_blocking_issue_when_required():
    result = decide_verdict("APPROVE", [], require_ticket=True, ticket_key=None)
    assert result.verdict == "REQUEST_CHANGES"
    assert any(i["title"] == NO_TICKET_TITLE for i in result.issues)
    assert result.issues[-1]["severity"] == "blocker"
    assert result.issues[-1]["line"] is None


def test_ticket_present_satisfies_the_rule():
    result = decide_verdict("APPROVE", [], require_ticket=True, ticket_key="ABC-12")
    assert result.verdict == "APPROVE"
    assert result.issues == []


def test_ticket_rule_off_by_default():
    assert decide_verdict("APPROVE", []).verdict == "APPROVE"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"title": "ABC-12 add widget"}, "ABC-12"),
        ({"title": "add widget", "branch": "feat/abc-7-cart"}, "ABC-7"),
        ({"title": "x", "branch": "y", "body": "Implements ABC_44."}, "ABC-44"),
        ({"commit_messages": ["fix: thing\n\nRefs: ABC-9"]}, "ABC-9"),
        ({"title": "XABC-12 is not a key"}, None),
        ({"title": "ABC-12345x"}, "ABC-12345"),
        ({"title": "nothing here"}, None),
    ],
)
def test_extract_ticket_key(kwargs, expected):
    assert extract_ticket_key(["ABC"], **kwargs) == expected


def test_extract_ticket_key_without_prefixes_matches_any_key():
    assert extract_ticket_key([], title="ZED-3 widget") == "ZED-3"
    assert extract_ticket_key([], title="utf-8 handling") is None


def test_title_wins_over_branch():
    assert extract_ticket_key(["ABC"], title="ABC-1", branch="ABC-2") == "ABC-1"


def test_guard_posted_verdict_refuses_approve_with_blocking_issue():
    verdict, overridden = guard_posted_verdict("APPROVE", [_issue("blocker")])
    assert (verdict, overridden) == ("REQUEST_CHANGES", True)


def test_guard_posted_verdict_leaves_everything_else_alone():
    assert guard_posted_verdict("APPROVE", [_issue("nit")]) == ("APPROVE", False)
    assert guard_posted_verdict("COMMENT", [_issue("blocker")]) == ("COMMENT", False)


def test_a_prior_blocker_the_model_never_mentions_still_blocks():
    # Silence is not "fixed": omitting a previous blocker must not approve.
    result = decide_verdict("APPROVE", [], prior_issues=[], prior_severities={11: "blocker"})
    assert result.verdict == "REQUEST_CHANGES"
    assert [p["comment_id"] for p in result.prior_blocking] == [11]
    assert result.prior_blocking[0]["status"] == "unreported"


def test_an_unknown_prior_status_still_blocks():
    prior = [{"comment_id": 11, "description": "x", "status": "maybe", "note": ""}]
    result = decide_verdict("APPROVE", [], prior_issues=prior, prior_severities={11: "major"})
    assert result.verdict == "REQUEST_CHANGES"


def test_resolved_ids_lists_only_what_the_model_reported_resolved():
    prior = [
        {"comment_id": 11, "description": "x", "status": "resolved", "note": ""},
        {"comment_id": 12, "description": "y", "status": "unresolved", "note": ""},
    ]
    result = decide_verdict(
        "COMMENT", [], prior_issues=prior, prior_severities={11: "major", 12: "major"}
    )
    assert result.resolved_ids == [11]
    assert result.verdict == "REQUEST_CHANGES"
