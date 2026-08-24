"""validate_agents.py --instance — validating the fleet that actually runs.

The validator's default mode checks git-TRACKED manifests only. Every real
instance manifest is gitignored (.gitignore: docs/agents/*.yaml), so on a box
running 25 manifests the CI job validates one. It was green because it checked
almost nothing — and on 2026-08-23, when a YAML typo in main.yaml took the
primary agent off the air for 3h48m, the only strict validator in the repo
had no opinion, because the broken file was not tracked.

--instance validates every manifest PRESENT, for on-box use (the daily
guardrail watch). The tracked-only default stays: the platform CI gate must
not fail on agents that live only in one operator's fleet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_agents.py"

GOOD = """\
id: probe-good
name: Probe
description: test fixture
version: "2026-08-24"
department: system
model:
  primary: openrouter/xiaomi/mimo-v2.5
schedule:
  cron: ""
delivery:
  mode: none
tools_allowed: []
instructions: test
"""

# The 2026-08-23 defect class: a mis-indented list item.
BROKEN = """\
id: probe-broken
model:
  fallbacks:
    - a
  - b
"""


def run_validator(manifest_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), "--manifest-dir", str(manifest_dir), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )


def test_instance_mode_validates_untracked_manifests(tmp_path: Path):
    """A gitignored/untracked manifest is invisible to the default mode and
    visible to --instance."""
    (tmp_path / "probe-good.yaml").write_text(GOOD)

    default = run_validator(tmp_path)
    assert "probe-good" not in default.stdout, (
        "default mode must stay tracked-only:\n" + default.stdout
    )

    instance = run_validator(tmp_path, "--instance")
    assert "probe-good" in instance.stdout, instance.stdout + instance.stderr


def test_instance_mode_reports_a_parse_failure_instead_of_crashing(tmp_path: Path):
    """On 2026-08-23 the broken main.yaml would have made this validator
    TRACEBACK, not report — yaml.safe_load was naked. A validator that crashes
    on the exact defect it exists to catch is decoration."""
    (tmp_path / "probe-good.yaml").write_text(GOOD)
    (tmp_path / "probe-broken.yaml").write_text(BROKEN)

    result = run_validator(tmp_path, "--instance")

    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, "a parse failure must fail the run"
    assert "probe-broken.yaml" in result.stdout, result.stdout
    combined = result.stdout.lower()
    assert "parse" in combined or "yaml" in combined, result.stdout


def test_parse_failure_names_the_file_not_the_operator_path(tmp_path: Path):
    """The report line goes into a paged/journaled report — bare filename only."""
    (tmp_path / "probe-broken.yaml").write_text(BROKEN)
    result = run_validator(tmp_path, "--instance")
    assert "probe-broken.yaml" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_instance_mode_json_carries_parse_failures(tmp_path: Path):
    (tmp_path / "probe-broken.yaml").write_text(BROKEN)
    result = run_validator(tmp_path, "--instance", "--json")
    data = json.loads(result.stdout[result.stdout.index("{") :])
    assert data["parse_failures"], data
    assert data["parse_failures"][0]["file"] == "probe-broken.yaml"


def test_default_mode_behaviour_is_unchanged():
    """The platform CI gate: tracked manifests only, exit 0 on this repo."""
    result = subprocess.run(
        ["python3", str(SCRIPT), "--ci"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
