"""The pr-review skill ships, parses, matches the code's schema, and stays generic."""

from __future__ import annotations

import json
from pathlib import Path

from robothor.engine.skills import _parse_skill_file
from robothor.pr_review.schema import REVIEW_OUTPUT_SCHEMA, validate_review_output

SKILL_DIR = Path(__file__).resolve().parents[3] / "agents" / "skills" / "pr-review"


def test_frontmatter_parses():
    defn = _parse_skill_file(SKILL_DIR / "SKILL.md")
    assert defn is not None
    assert defn.name == "pr-review"
    assert defn.description
    assert defn.output_format == "json"
    assert "blockers and high issues" in defn.content


def test_skill_schema_is_the_code_schema():
    assert json.loads((SKILL_DIR / "schema.json").read_text()) == REVIEW_OUTPUT_SCHEMA


def test_meta_marks_it_platform():
    assert json.loads((SKILL_DIR / "meta.json").read_text()) == {"origin": "platform"}


def test_the_brief_is_short_and_has_a_finish_line():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert len(text.splitlines()) < 150  # a brief, not a manual
    for phrase in (
        "Not a finding",
        "Verify every finding",
        "Severity floor",
        "Read the GitHub state first",
        "Re-reviewing: the finish line",
        "follow-up ticket",
    ):
        assert phrase in text
    for gone in ("twelve lenses", "Completeness pass", "words-match-code sweep"):
        assert gone not in text
    for severity in ("blocker", "major"):
        assert f"**{severity}**" in text


def test_no_org_specific_content():
    text = (SKILL_DIR / "SKILL.md").read_text().lower()
    for banned in ("pharmac", "jira", "atlassian", "medusa", "php", "#1", "cloudid"):
        assert banned not in text, banned


def test_schema_validator_accepts_a_good_result_and_rejects_bad_ones():
    good = {
        "verdict": "APPROVE",
        "summary": "s",
        "issues": [
            {
                "path": "a.py",
                "line": None,
                "start_line": None,
                "side": "RIGHT",
                "severity": "nit",
                "title": "t",
                "body": "b",
            }
        ],
        "prior_issues": [{"comment_id": 3, "description": "d", "status": "resolved", "note": ""}],
    }
    assert validate_review_output(good) == []
    assert validate_review_output(None) == ["output must be an object"]
    bad = json.loads(json.dumps(good))
    bad["issues"][0]["severity"] = "critical"
    bad["issues"][0]["line"] = True
    bad["extra"] = 1
    errors = validate_review_output(bad)
    assert any("severity" in e for e in errors)
    assert any("line" in e for e in errors)
    assert any("extra" in e for e in errors)


def test_accepted_is_a_valid_previous_finding_status():
    out = {
        "verdict": "APPROVE",
        "summary": "s",
        "issues": [],
        "prior_issues": [{"comment_id": 3, "description": "d", "status": "accepted", "note": "n"}],
    }
    assert validate_review_output(out) == []
