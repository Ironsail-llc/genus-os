"""The gate for CLAUDE.md rule #1 — no instance data in platform code.

It had no tests at all, and on 2026-08-23 it passed a real leak:
infra/tmpfiles/robothor-restart.conf shipped `d /run/robothor/restart-requests
0700 philip philip -` into git. Two independent holes let that through.

  1. scripts/instance_leak_allowlist.yaml had globally exempted the operator's
     own workspace path (slash-home-slash-<operator>-slash-robothor, not spelled
     literally here or this very file would trip the gate — which the CI run
     that caught the first version of this docstring proved) across the entire
     tracked tree since 2026-05-28.
     The hook ran and passed; it was not bypassed. That is worse.
  2. There was no pattern for a bare POSITIONAL account. tmpfiles.d rows spell
     the user and group as columns 4 and 5 with no `User=` prefix, so neither
     the unit-file convention nor the /home/<user>/ pattern could see them.

And it was a pre-commit hook only, with no CI backstop, so `--no-verify` or a
commit made through the web UI skipped it entirely.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_instance_leak.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_instance_leak", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CIL = _module()


def check(path: str, content: str, allowlist: set[str] | None = None) -> list[str]:
    return CIL._check_file(path, content, allowlist if allowlist is not None else set())


def real_allowlist() -> set[str]:
    return CIL._load_allowlist()


# ── The positional-account hole ──────────────────────────────────────────────


def test_a_bare_account_in_a_tmpfiles_row_is_a_leak():
    warnings = check("infra/tmpfiles/x.conf", "d /run/x 0700 philip philip -\n")
    assert len(warnings) == 2, warnings
    assert "user column" in warnings[0]
    assert "group column" in warnings[1]


def test_the_placeholder_account_is_clean():
    assert check("infra/tmpfiles/x.conf", "d /run/robothor/r 0700 robothor robothor -\n") == []


@pytest.mark.parametrize("account", ["postgres", "root", "nobody", "-"])
def test_real_system_accounts_are_permitted(account: str):
    assert check("infra/tmpfiles/x.conf", f"d /var/lib/x 0755 {account} {account} -\n") == []


def test_the_word_robothor_elsewhere_is_not_an_account():
    """The repo says `robothor` everywhere; only the account COLUMNS matter.

    This is why the pattern matches the SHAPE of a tmpfiles row rather than
    the word — a word-based rule would drown in false positives.
    """
    for line in (
        'p = Path("/run/robothor/restart-requests")\n',
        "WantedBy=multi-user.target\n",
        "Run `robothor init` in /opt/robothor to start.\n",
        "drwxrwxr-x 4 robothor robothor 4096 May 28 15:18 agents\n",
    ):
        assert check("robothor/engine/x.py", line) == [], line


def test_a_unit_user_line_naming_an_instance_account_is_a_leak():
    assert len(check("infra/systemd/x.service", "User=philip\n")) == 1


def test_a_python_group_assignment_is_not_a_unit_account():
    """Scoping the User=/Group= rule to unit-shaped files exists to prevent
    exactly this false positive."""
    assert check("robothor/models.py", "Group = None\n") == []


# ── The allowlist hole ───────────────────────────────────────────────────────


def test_the_allowlist_does_not_whitelist_an_operator_home():
    """Only the template placeholder may be exempt.

    The username is derived from the file rather than hardcoded — writing
    /home/<operator> into a test would itself be the leak this gate exists to
    stop.
    """
    text = (REPO_ROOT / "scripts" / "instance_leak_allowlist.yaml").read_text()
    homes = set(re.findall(r"/home/[A-Za-z0-9._-]+", text))
    assert homes <= {"/home/robothor"}, f"operator home allowlisted: {sorted(homes)}"


def test_tracked_infra_files_are_clean():
    """End-to-end, against the real allowlist and the real tree."""
    allowlist = real_allowlist()
    tracked = subprocess.run(
        ["git", "ls-files", "--", "infra/"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.splitlines()
    for rel in tracked:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert check(rel, content, allowlist) == [], rel


# ── The missing CI backstop ──────────────────────────────────────────────────


def test_the_leak_gate_runs_in_ci():
    """A pre-commit hook is bypassable with --no-verify and never runs on a
    commit made through the web UI."""
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    runs = [step.get("run", "") for job in wf["jobs"].values() for step in job.get("steps", [])]
    assert any("check_instance_leak.py" in r for r in runs), (
        "no CI job runs scripts/check_instance_leak.py"
    )


def test_the_leak_gate_job_has_full_history():
    """--ci diffs against the merge base; a shallow checkout has none."""
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    for job in wf["jobs"].values():
        steps = job.get("steps", [])
        if not any("check_instance_leak.py" in s.get("run", "") for s in steps):
            continue
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        assert checkout.get("with", {}).get("fetch-depth") == 0
        return
    pytest.fail("no job runs the leak gate")


class TestPhonePatternPrecision:
    """The US-phone regex fired inside UUID-shaped test fixtures
    ('00000000-0000-0000-0000-…' contains 000-000 0000), which made the CI
    gate flag any PR that merely TOUCHED a file with a UUID literal — the
    first victim was the runner decomposition, for retargeting a patch in a
    test whose fixtures it never changed. A gate that cries wolf on fixtures
    gets bypassed; precision is part of the control."""

    def test_uuid_fixture_ids_are_not_phone_numbers(self):
        for line in (
            'id="00000000-0000-0000-0000-0000000000rv",\n',
            'run_id = "00000000-0000-0000-0000-00000006cb7e"\n',
            "correlation: 123e4567-e89b-12d3-a456-426614174000\n",
        ):
            assert check("robothor/engine/tests/x.py", line) == [], line

    def test_a_real_phone_number_is_still_caught(self):
        # Assembled at runtime so THIS source file's own line is not
        # phone-shaped — the gate scans changed files, this one included, and
        # its first CI run flagged its own fixtures. (555 = reserved block.)
        number = "-".join(["415", "555", "2671"])
        assert check("robothor/notes.py", f"call me at {number}\n"), (
            "the boundary guards must not blind the gate to real numbers"
        )

    def test_parenthesized_phone_is_still_caught(self):
        number = "(" + "212" + ") " + "867" + "-" + "5309"
        assert check("robothor/notes.py", f"office: {number}\n")
