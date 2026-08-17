"""Tests for scripts/check_npm_audit.py -- the named-allowlist gate for
`npm audit --json` output (see .npm-audit-allowlist.json)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.check_npm_audit import main

if TYPE_CHECKING:
    from pathlib import Path

ALLOWED_ID = "GHSA-aaaa-bbbb-cccc"
EXTRA_HIGH_ID = "GHSA-dddd-eeee-ffff"
MODERATE_ID = "GHSA-gggg-hhhh-iiii"

ALLOWLIST = [
    {
        "id": ALLOWED_ID,
        "package": "some-bundled-dep",
        "reason": "test fixture: reviewed and accepted",
        "added": "2026-08-17",
    }
]


def _advisory(ghsa_id: str, severity: str) -> dict:
    return {
        "source": 1,
        "title": f"fake advisory {ghsa_id}",
        "url": f"https://github.com/advisories/{ghsa_id}",
        "severity": severity,
    }


def _report(vulnerabilities: dict) -> dict:
    """A real, successful `npm audit --json` report always carries both a
    "vulnerabilities" and a "metadata" key -- shape the fixtures the same
    way so these tests exercise the allowlist logic, not the shape check."""
    return {
        "vulnerabilities": vulnerabilities,
        "metadata": {"vulnerabilities": {"total": len(vulnerabilities)}},
    }


def _write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _run(tmp_path: Path, report: dict, allowlist: list[dict]) -> int:
    report_path = _write(tmp_path, "audit.json", report)
    allowlist_path = _write(tmp_path, "allowlist.json", allowlist)
    return main(["check_npm_audit.py", str(report_path), "--allowlist", str(allowlist_path)])


def test_only_allowlisted_ids_exits_zero(tmp_path, capsys):
    report = _report(
        {
            "some-bundled-dep": {
                "name": "some-bundled-dep",
                "severity": "high",
                "via": [_advisory(ALLOWED_ID, "high")],
            }
        }
    )
    result = _run(tmp_path, report, ALLOWLIST)
    out = capsys.readouterr().out
    assert result == 0, out
    assert "ALLOWED" in out
    assert ALLOWED_ID in out


def test_extra_unallowlisted_high_exits_one(tmp_path, capsys):
    report = _report(
        {
            "some-bundled-dep": {
                "name": "some-bundled-dep",
                "severity": "high",
                "via": [_advisory(ALLOWED_ID, "high")],
            },
            "some-other-dep": {
                "name": "some-other-dep",
                "severity": "high",
                "via": [_advisory(EXTRA_HIGH_ID, "high")],
            },
        }
    )
    result = _run(tmp_path, report, ALLOWLIST)
    out = capsys.readouterr().out
    assert result == 1, out
    assert "FAIL" in out
    assert EXTRA_HIGH_ID in out
    assert "some-other-dep" in out


def test_unallowlisted_moderate_exits_zero(tmp_path, capsys):
    """The gate is high+; a moderate advisory needs no allowlist entry at all."""
    report = _report(
        {
            "some-moderate-dep": {
                "name": "some-moderate-dep",
                "severity": "moderate",
                "via": [_advisory(MODERATE_ID, "moderate")],
            }
        }
    )
    result = _run(tmp_path, report, ALLOWLIST)
    out = capsys.readouterr().out
    assert result == 0, out
    assert "FAIL" not in out


def test_audit_error_shaped_json_exits_one(tmp_path, capsys):
    """`npm audit --json` writes valid JSON even when the audit tool itself
    failed (e.g. registry unreachable) -- something like
    {"message": "...ECONNREFUSED...", "error": {...}}, with NO
    "vulnerabilities" key. Piped through the workflow's `|| true`, that must
    NOT read as "zero vulnerabilities found" -- it has to fail closed."""
    report = {
        "message": "npm error code ECONNREFUSED\nnpm error network request failed",
        "error": {
            "code": "ECONNREFUSED",
            "summary": "request to https://registry.npmjs.org/-/npm/v1/security/audits failed",
            "detail": "",
        },
    }
    result = _run(tmp_path, report, ALLOWLIST)
    err = capsys.readouterr().err
    assert result == 1, err
    assert "FAIL" in err


def test_keyless_but_valid_json_exits_one(tmp_path, capsys):
    """Valid JSON with neither "vulnerabilities" nor "metadata" isn't a real
    audit report -- fail closed rather than defaulting to "no violations"."""
    result = _run(tmp_path, {}, ALLOWLIST)
    err = capsys.readouterr().err
    assert result == 1, err
    assert "FAIL" in err
