"""validate_agents.py checks workflow budgets, and --ci makes that a failure.

`workflow_budget.check_step_budgets` shipped with a `strict=True` parameter and
no production caller: a grep over the tree found exactly one call site, and it
was warn-only. Per this repo's own probe-don't-trust-silence rule, a flag with
no caller is a control that cannot be shown to work, so the error half of the
validator had never fired anywhere.

It is wired here rather than only in the engine loader because the loader runs
on a box, at startup, into a log nobody reads at PR time — and because
`docs/workflows/*.yaml` are TRACKED platform files, so a budget inversion can
ship in a commit. `--ci` implies strict: an inversion the check can actually
resolve should fail the PR.

The other half of what these pin is the COUNT. On a clean platform checkout
every agent manifest is gitignored, so nothing resolves and the check has
nothing to say — which must not look like a clean bill of health.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_agents.py"

#: Three cloud models plus the local tail — the chain from the 2026-09-13
#: incident. Worth 300 + 300 + 300 + 600 plus one 300s primary retry = 1800s.
INCIDENT_MANIFEST = {
    "model": {
        "primary": "openrouter/deepseek/deepseek-v4.1-flash",
        "fallbacks": [
            "openrouter/xiaomi/mimo-v2.5",
            "openrouter/deepseek/deepseek-v4-flash",
            "ollama_chat/qwen3.8:27b",
        ],
    }
}


@pytest.fixture
def validator():
    spec = importlib.util.spec_from_file_location("validate_agents_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _workflow(tmp_path: Path, timeout_seconds: int) -> Path:
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "probe-pipeline.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "probe-pipeline",
                "name": "Probe pipeline",
                "timeout_seconds": timeout_seconds,
                "steps": [
                    {"id": "classify", "type": "agent", "agent_id": "probe-classifier"},
                ],
            }
        )
    )
    return wf_dir


class TestTheStrictHalfActuallyFires:
    def test_an_inversion_fails_under_strict(self, validator, tmp_path, capsys):
        validator.WORKFLOW_DIR = _workflow(tmp_path, 900)
        failures = validator.check_workflow_budgets(
            {"probe-classifier": INCIDENT_MANIFEST}, strict=True
        )
        assert failures == 1, "strict mode did not fail on a budget it could resolve"
        out = capsys.readouterr().out
        assert "1800s" in out and "900s" in out and "classify" in out

    def test_the_same_inversion_is_only_a_warning_without_strict(self, validator, tmp_path):
        validator.WORKFLOW_DIR = _workflow(tmp_path, 900)
        assert (
            validator.check_workflow_budgets({"probe-classifier": INCIDENT_MANIFEST}, strict=False)
            == 0
        )

    def test_a_coherent_budget_passes_under_strict(self, validator, tmp_path):
        """Guards against the check firing on everything, which is how a gate
        gets turned off and left off."""
        validator.WORKFLOW_DIR = _workflow(tmp_path, 2400)
        assert (
            validator.check_workflow_budgets({"probe-classifier": INCIDENT_MANIFEST}, strict=True)
            == 0
        )


class TestItSaysWhatItCouldNotCheck:
    def test_an_unresolvable_chain_is_counted_and_reported(self, validator, tmp_path, capsys):
        validator.WORKFLOW_DIR = _workflow(tmp_path, 900)
        # No manifest for probe-classifier — the platform-checkout case.
        failures = validator.check_workflow_budgets({}, strict=True)
        assert failures == 0, "an UNCHECKED step must not be reported as a failure"
        out = capsys.readouterr().out
        assert "0 of 1 agent step(s) checked" in out, out
        assert "1 chain(s) unresolved" in out, out

    def test_the_platform_checkout_reports_its_own_workflows(self, validator, capsys):
        """The real files, through the real entry point's helper."""
        validator.check_workflow_budgets({}, strict=True)
        out = capsys.readouterr().out
        assert "Workflow budgets:" in out
        assert "workflow(s)" in out


class TestTheShippedWorkflowsPassTheShippedGate:
    def test_the_tracked_workflows_clear_the_incident_chain(self, validator, capsys):
        """`docs/workflows/*.yaml` are tracked platform files. Shipping a
        validator whose first warnings fire against files in the same commit
        is not a gate, it is noise."""
        manifests = dict.fromkeys(
            (
                "email-classifier",
                "email-responder",
                "calendar-agent",
                "vision-agent",
                "devops-analyst",
                "main",
            ),
            INCIDENT_MANIFEST,
        )
        failures = validator.check_workflow_budgets(manifests, strict=True)
        assert failures == 0, capsys.readouterr().out
