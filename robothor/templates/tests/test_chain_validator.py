"""Tests for chain_validator — full-chain validation checks M-R."""

import pytest
import yaml

from robothor.templates.chain_validator import (
    check_delivery_coherence,
    check_event_path,
    check_pipeline_continuity,
    check_tag_flow,
    check_tool_instruction_coherence,
    check_workflow_coverage,
    validate_chain,
)


@pytest.fixture
def chain_repo(tmp_path):
    """Create a repo structure for chain validation tests."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # brain/scripts/
    scripts = repo / "brain" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "email_sync.py").write_text(
        'from robothor.event_bus import publish\npublish("email", {"type": "email.new"})\n'
    )

    # brain/agents/
    agents_md = repo / "brain" / "agents"
    agents_md.mkdir(parents=True)
    (agents_md / "EMAIL_CLASSIFIER.md").write_text(
        "# Email Classifier\n\n"
        "Use `list_my_tasks` to check your inbox.\n"
        "Use `create_task` to route to downstream.\n"
        "Use `resolve_task` when done.\n"
    )
    (agents_md / "SILENT_AGENT.md").write_text(
        "# Silent Agent\n\nUse `send_message` to notify the user.\n"
    )

    # docs/agents/
    (repo / "docs" / "agents").mkdir(parents=True)

    # docs/workflows/
    workflows = repo / "docs" / "workflows"
    workflows.mkdir(parents=True)
    wf = {
        "id": "nightwatch",
        "steps": [
            {"id": "analyze", "type": "agent", "agent": "improvement-analyst"},
            {"id": "fix", "type": "agent", "agent": "overnight-pr"},
        ],
    }
    (workflows / "nightwatch.yaml").write_text(yaml.dump(wf))

    return repo


class TestCheckEventPath:
    def test_pass_when_script_publishes(self, chain_repo):
        manifest = {"hooks": [{"stream": "email", "event_type": "email.new"}]}
        result = check_event_path(manifest, chain_repo)
        assert result.status == "PASS"

    def test_warn_when_no_script_publishes(self, chain_repo):
        manifest = {"hooks": [{"stream": "calendar", "event_type": "calendar.new"}]}
        result = check_event_path(manifest, chain_repo)
        assert result.status == "WARN"
        assert "calendar" in result.message

    def test_skip_when_no_hooks(self, chain_repo):
        manifest = {}
        result = check_event_path(manifest, chain_repo)
        assert result.status == "SKIP"

    def test_skip_when_no_scripts_dir(self, tmp_path):
        manifest = {"hooks": [{"stream": "email", "event_type": "email.new"}]}
        result = check_event_path(manifest, tmp_path)
        assert result.status == "SKIP"


class TestCheckPipelineContinuity:
    def test_pass_when_bidirectional(self):
        all_manifests = {
            "classifier": {
                "id": "classifier",
                "creates_tasks_for": ["responder"],
            },
            "responder": {
                "id": "responder",
                "receives_tasks_from": ["classifier"],
                "task_protocol": True,
            },
        }
        result = check_pipeline_continuity(all_manifests["classifier"], all_manifests)
        assert result.status == "PASS"

    def test_warn_when_target_missing_receives_from(self):
        all_manifests = {
            "classifier": {
                "id": "classifier",
                "creates_tasks_for": ["responder"],
            },
            "responder": {
                "id": "responder",
                "receives_tasks_from": [],
                "task_protocol": True,
            },
        }
        result = check_pipeline_continuity(all_manifests["classifier"], all_manifests)
        assert result.status == "WARN"
        assert "receives_tasks_from" in result.details[0]

    def test_warn_when_target_missing_task_protocol(self):
        all_manifests = {
            "classifier": {
                "id": "classifier",
                "creates_tasks_for": ["responder"],
            },
            "responder": {
                "id": "responder",
                "receives_tasks_from": ["classifier"],
            },
        }
        result = check_pipeline_continuity(all_manifests["classifier"], all_manifests)
        assert result.status == "WARN"
        assert "task_protocol" in result.details[0]

    def test_warn_when_target_has_no_manifest(self):
        all_manifests = {
            "classifier": {
                "id": "classifier",
                "creates_tasks_for": ["nonexistent"],
            },
        }
        result = check_pipeline_continuity(all_manifests["classifier"], all_manifests)
        assert result.status == "WARN"

    def test_skip_when_no_creates_tasks_for(self):
        manifest = {"id": "standalone"}
        result = check_pipeline_continuity(manifest, {})
        assert result.status == "SKIP"


class TestCheckWorkflowCoverage:
    def test_pass_when_agent_has_cron(self, chain_repo):
        manifest = {
            "id": "improvement-analyst",
            "schedule": {"cron": "0 2 * * *"},
        }
        result = check_workflow_coverage(manifest, chain_repo)
        assert result.status == "PASS"

    def test_warn_when_no_trigger(self, chain_repo):
        manifest = {"id": "improvement-analyst"}
        result = check_workflow_coverage(manifest, chain_repo)
        assert result.status == "WARN"

    def test_skip_when_not_in_workflow(self, chain_repo):
        manifest = {"id": "unrelated-agent", "schedule": {"cron": "0 * * * *"}}
        result = check_workflow_coverage(manifest, chain_repo)
        assert result.status == "SKIP"

    def test_skip_when_no_workflows_dir(self, tmp_path):
        manifest = {"id": "test"}
        result = check_workflow_coverage(manifest, tmp_path)
        assert result.status == "SKIP"


class TestCheckToolInstructionCoherence:
    def test_pass_when_tools_match(self, chain_repo):
        manifest = {
            "instruction_file": "brain/agents/EMAIL_CLASSIFIER.md",
            "tools_allowed": ["list_my_tasks", "create_task", "resolve_task"],
        }
        result = check_tool_instruction_coherence(manifest, chain_repo)
        assert result.status == "PASS"

    def test_warn_when_tool_missing_from_allowed(self, chain_repo):
        manifest = {
            "instruction_file": "brain/agents/EMAIL_CLASSIFIER.md",
            "tools_allowed": ["list_my_tasks"],  # Missing create_task and resolve_task
        }
        result = check_tool_instruction_coherence(manifest, chain_repo)
        assert result.status == "WARN"
        assert "create_task" in result.message or "resolve_task" in result.message

    def test_skip_when_no_instruction_file(self, chain_repo):
        manifest = {"tools_allowed": ["exec"]}
        result = check_tool_instruction_coherence(manifest, chain_repo)
        assert result.status == "SKIP"

    def test_skip_when_no_tools_allowed(self, chain_repo):
        manifest = {"instruction_file": "brain/agents/EMAIL_CLASSIFIER.md"}
        result = check_tool_instruction_coherence(manifest, chain_repo)
        assert result.status == "SKIP"


class TestCheckTagFlow:
    def test_pass_when_tags_consumed(self):
        all_manifests = {
            "producer": {
                "id": "producer",
                "tags_produced": ["email", "reply-needed"],
            },
            "consumer": {
                "id": "consumer",
                "tags_consumed": ["email", "reply-needed"],
            },
        }
        result = check_tag_flow(all_manifests["producer"], all_manifests)
        assert result.status == "PASS"

    def test_warn_when_orphaned_tags(self):
        all_manifests = {
            "producer": {
                "id": "producer",
                "tags_produced": ["email", "orphan-tag"],
            },
        }
        result = check_tag_flow(all_manifests["producer"], all_manifests)
        assert result.status == "WARN"
        assert "orphan-tag" in result.message

    def test_skip_when_no_tags_produced(self):
        manifest = {"id": "no-tags"}
        result = check_tag_flow(manifest, {})
        assert result.status == "SKIP"


class TestCheckDeliveryCoherence:
    def test_warn_when_none_with_send_message(self, chain_repo):
        manifest = {
            "delivery": {"mode": "none"},
            "instruction_file": "brain/agents/SILENT_AGENT.md",
        }
        result = check_delivery_coherence(manifest, chain_repo)
        assert result.status == "WARN"
        assert "send_message" in result.message

    def test_skip_when_delivery_not_none(self, chain_repo):
        manifest = {
            "delivery": {"mode": "announce"},
            "instruction_file": "brain/agents/SILENT_AGENT.md",
        }
        result = check_delivery_coherence(manifest, chain_repo)
        assert result.status == "SKIP"

    def test_pass_when_no_comm_tools(self, chain_repo):
        manifest = {
            "delivery": {"mode": "none"},
            "instruction_file": "brain/agents/EMAIL_CLASSIFIER.md",
        }
        result = check_delivery_coherence(manifest, chain_repo)
        assert result.status == "PASS"


class TestValidateChain:
    def test_returns_six_results(self, chain_repo):
        manifest = {"id": "test-agent"}
        results = validate_chain(manifest, {"test-agent": manifest}, repo_root=chain_repo)
        assert len(results) == 6
        assert all(hasattr(r, "check_id") for r in results)

    def test_check_ids_are_m_through_r(self, chain_repo):
        manifest = {"id": "test"}
        results = validate_chain(manifest, {"test": manifest}, repo_root=chain_repo)
        check_ids = [r.check_id for r in results]
        assert check_ids == ["M", "N", "O", "P", "Q", "R"]
