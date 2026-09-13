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


class TestAGreenJobIsEvidence:
    """Review N5: the first version could only ever check an INSTANCE. On a
    platform checkout — the only place `validate-agents` runs — every manifest
    is gitignored, nothing resolved, and the job exited 0 having checked
    nothing. A green gate that cannot fail is not a gate.

    The fix is that an unresolved chain falls back to `REFERENCE_CHAIN`, the
    four-model shape the platform ships, so the TRACKED workflow budgets are
    really checked here."""

    def test_an_unresolved_chain_is_checked_against_the_reference_shape(
        self, validator, tmp_path, capsys
    ):
        validator.WORKFLOW_DIR = _workflow(tmp_path, 900)
        # No manifest for probe-classifier — the platform-checkout case.
        failures = validator.check_workflow_budgets({}, strict=True)
        assert failures == 1, (
            "a 900s budget went unchecked because the agent manifest is gitignored — "
            "which is every agent manifest, on every platform checkout"
        )
        out = capsys.readouterr().out
        assert "1 agent step(s) checked" in out, out
        assert "reference chain" in out, out

    def test_a_coherent_budget_still_passes_against_the_reference_shape(self, validator, tmp_path):
        validator.WORKFLOW_DIR = _workflow(tmp_path, 2400)
        assert validator.check_workflow_budgets({}, strict=True) == 0

    def test_a_declared_chain_wins_over_the_reference(self, validator, tmp_path, capsys):
        """The instance's own config is the better answer when it is readable."""
        validator.WORKFLOW_DIR = _workflow(tmp_path, 900)
        short = {"model": {"primary": "openrouter/only"}}  # 300 + 300 = 600 < 900
        assert validator.check_workflow_budgets({"probe-classifier": short}, strict=True) == 0
        out = capsys.readouterr().out
        assert "1 against declared chains" in out, out

    def test_checking_nothing_at_all_is_reported_as_a_failure(self, validator, tmp_path, capsys):
        """The backstop: reaching zero now means the workflows or the fallback
        went missing, not that the instance is unusual."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "no-agents.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "no-agent-steps",
                    "timeout_seconds": 900,
                    "steps": [{"id": "shape", "type": "transform", "expression": "1"}],
                }
            )
        )
        validator.WORKFLOW_DIR = wf_dir
        assert validator.check_workflow_budgets({}, strict=True) == 1
        assert "not evidence of anything" in capsys.readouterr().out

    def test_the_platform_checkout_checks_every_tracked_step(self, validator, capsys):
        """The real files, through the real entry point's helper."""
        failures = validator.check_workflow_budgets({}, strict=True)
        out = capsys.readouterr().out
        assert "0 agent step(s) checked" not in out, out
        assert failures == 0, out


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
