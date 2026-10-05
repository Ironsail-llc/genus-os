"""The pr-reviewer agent template and its two workflow templates load and validate."""

from __future__ import annotations

from pathlib import Path

import yaml

from robothor.engine.manifest_schema import errors, validate
from robothor.engine.models import WorkflowStepType
from robothor.engine.tools.constants import OPT_IN_TOOLS
from robothor.engine.workflow import parse_workflow
from robothor.templates.resolver import TemplateResolver

REPO = Path(__file__).resolve().parents[3]
BUNDLE = REPO / "templates" / "agents" / "engineering" / "pr-reviewer"
WORKFLOWS = REPO / "templates" / "workflows"


def _render() -> dict:
    resolver = TemplateResolver()
    defaults = yaml.safe_load((REPO / "templates/agents/_defaults.yaml").read_text()) or {}
    setup = yaml.safe_load((BUNDLE / "setup.yaml").read_text()) or {}
    context = resolver.build_context(setup_yaml=setup, defaults_yaml=defaults)
    return yaml.safe_load(
        resolver.resolve_file(BUNDLE / "manifest.template.yaml", context, trusted_root=BUNDLE)
    )


def test_manifest_validates_strictly():
    data = _render()
    found = errors(validate(data, strict=True))
    assert not found, found
    assert data["id"] == "pr-reviewer"
    assert data["task_protocol"] is True
    assert "model" not in data  # the fleet default applies


def test_manifest_opts_into_exactly_what_the_agent_calls():
    data = _render()
    opt_in = set(data["tools_opt_in"])
    assert opt_in <= OPT_IN_TOOLS
    # prepare starts the job and finalize posts through the GitHub handlers
    # directly: the agent itself can neither start an arbitrary Claude Code job
    # nor post, reply on or resolve a review.
    assert opt_in == {
        "claude_code_status",
        "claude_code_wait",
        "claude_code_cancel",
        "pr_review_prepare",
        "pr_review_finalize",
    }


def test_manifest_gives_the_long_tools_room():
    from robothor.engine.tool_timeouts import resolve_tool_timeout

    data = _render()
    configured = int((data.get("v2") or {}).get("tool_timeout_seconds", 120))
    # claude_code_wait(timeout_s=1200) as instructed, and a first clone of a large repo.
    assert resolve_tool_timeout("claude_code_wait", configured) >= 1200 + 30
    assert resolve_tool_timeout("pr_review_prepare", configured) >= 600
    assert resolve_tool_timeout("pr_review_finalize", configured) >= 300


def test_catalog_lists_the_agent():
    catalog = yaml.safe_load((REPO / "templates/agents/_catalog.yaml").read_text())
    assert "pr-reviewer" in catalog["departments"]["engineering"]["agents"]


def test_bundle_metadata_validates():
    from robothor.templates.validators import validate_skill_md

    problems = [e for e in validate_skill_md(BUNDLE) if e.severity == "error"]
    assert not problems, problems


def test_intake_workflow_is_one_tool_step():
    wf = parse_workflow(yaml.safe_load((WORKFLOWS / "pr-review-intake.yaml").read_text()))
    assert wf.id == "pr-review-intake"
    assert [t.cron for t in wf.triggers if t.type == "cron"] == ["*/2 * * * *"]
    [step] = wf.steps
    assert step.type == WorkflowStepType.TOOL and step.tool_name == "pr_review_intake"


def test_run_workflow_wakes_the_agent_only_when_queued():
    from robothor.engine.workflow import _eval_condition, _render_template

    wf = parse_workflow(yaml.safe_load((WORKFLOWS / "pr-review-run.yaml").read_text()))
    queue, cond, review, done = wf.steps
    # Counting only: the run workflow never dispatches; only the intake does.
    assert queue.tool_name == "pr_review_intake" and queue.tool_args == {"count_only": True}
    assert review.agent_id == "pr-reviewer"

    def branch(tool_output: dict) -> str:
        ctx = {"steps": {"queue": {"tool_output": tool_output}}}
        value = _render_template(cond.input_expr, ctx)
        for b in cond.branches:
            if b.otherwise or _eval_condition(b.when, value):
                return b.goto
        return ""

    assert branch({"queued_tasks": 2}) == "review"
    assert branch({"queued_tasks": 0}) == "done"
    assert branch({"error": "boom"}) == "done"
