"""Tests for wizard — intent capture and build plan generation."""

from robothor.templates.wizard import (
    AgentIntent,
    BuildPlan,
    generate_plan,
    plan_to_scaffold,
    select_model_tier,
    select_orchestration_pattern,
    select_tool_profile,
)


class TestSelectOrchestrationPattern:
    def test_sub_agent_returns_c(self):
        intent = AgentIntent(trigger_type="sub-agent")
        assert select_orchestration_pattern(intent) == "C"

    def test_interactive_returns_c(self):
        intent = AgentIntent(trigger_type="interactive")
        assert select_orchestration_pattern(intent) == "C"

    def test_event_with_data_sources_returns_d(self):
        intent = AgentIntent(trigger_type="event", data_sources=["email.new"])
        assert select_orchestration_pattern(intent) == "D"

    def test_event_without_data_sources_returns_a(self):
        intent = AgentIntent(trigger_type="event")
        assert select_orchestration_pattern(intent) == "A"

    def test_workflow_chain_returns_b(self):
        intent = AgentIntent(
            trigger_type="cron",
            upstream_agents=["analyzer"],
            downstream_agents=["responder"],
            existing_pipeline="nightwatch",
        )
        assert select_orchestration_pattern(intent) == "B"

    def test_cron_with_downstream_returns_a(self):
        intent = AgentIntent(trigger_type="cron", downstream_agents=["responder"])
        assert select_orchestration_pattern(intent) == "A"

    def test_cron_with_upstream_returns_d(self):
        intent = AgentIntent(trigger_type="cron", upstream_agents=["classifier"])
        assert select_orchestration_pattern(intent) == "D"

    def test_pure_cron_returns_a(self):
        intent = AgentIntent(trigger_type="cron")
        assert select_orchestration_pattern(intent) == "A"


class TestSelectToolProfile:
    def test_git_output_gets_git_tools(self):
        intent = AgentIntent(outputs=["draft PR", "commit fix"])
        tools = select_tool_profile(intent)
        assert "git_status" in tools
        assert "create_pull_request" in tools

    def test_email_output_gets_action_tools(self):
        intent = AgentIntent(outputs=["email reply", "notification"])
        tools = select_tool_profile(intent)
        assert "create_message" in tools

    def test_downstream_gets_crm_tools(self):
        intent = AgentIntent(downstream_agents=["responder"], outputs=["task routing"])
        tools = select_tool_profile(intent)
        assert "create_task" in tools
        assert "resolve_task" in tools

    def test_readonly_agent(self):
        intent = AgentIntent(outputs=["analysis report"])
        tools = select_tool_profile(intent)
        assert "read_file" in tools
        # Read-only should not have write tools
        assert "write_file" not in tools

    def test_web_data_source_adds_web_tools(self):
        intent = AgentIntent(
            data_sources=["web API", "http endpoint"],
            downstream_agents=["processor"],
            outputs=["task updates"],
        )
        tools = select_tool_profile(intent)
        assert "web_fetch" in tools


class TestSelectModelTier:
    def test_simple_gets_t0(self):
        intent = AgentIntent(complexity="simple")
        var, rationale = select_model_tier(intent)
        assert var == "model_primary"
        assert "T0" in rationale

    def test_moderate_gets_t1(self):
        intent = AgentIntent(complexity="moderate")
        var, rationale = select_model_tier(intent)
        assert var == "model_primary"
        assert "T1" in rationale

    def test_complex_sub_agent_gets_t3(self):
        intent = AgentIntent(complexity="complex", trigger_type="sub-agent")
        var, rationale = select_model_tier(intent)
        assert var == "model_quality"
        assert "T3" in rationale

    def test_complex_with_downstream_gets_t3(self):
        intent = AgentIntent(complexity="complex", downstream_agents=["worker"])
        var, rationale = select_model_tier(intent)
        assert var == "model_quality"
        assert "T3" in rationale

    def test_complex_standalone_gets_t2(self):
        intent = AgentIntent(complexity="complex")
        var, rationale = select_model_tier(intent)
        assert var == "model_quality"
        assert "T2" in rationale


class TestGeneratePlan:
    def test_plan_has_all_fields(self):
        intent = AgentIntent(
            purpose="Classify incoming emails",
            trigger_type="event",
            data_sources=["email.new"],
            downstream_agents=["email-responder"],
            complexity="simple",
        )
        plan = generate_plan(intent)
        assert isinstance(plan, BuildPlan)
        assert plan.pattern in ("A", "B", "C", "D")
        assert plan.model_tier in ("model_primary", "model_quality")
        assert len(plan.tool_list) > 0
        assert plan.manifest.get("description") == "Classify incoming emails"

    def test_event_plan_has_hooks(self):
        intent = AgentIntent(
            trigger_type="event",
            data_sources=["email.new"],
        )
        plan = generate_plan(intent)
        assert plan.hook_config
        assert plan.hook_config[0]["stream"] == "email"
        assert plan.hook_config[0]["event_type"] == "email.new"

    def test_task_receiver_has_task_protocol(self):
        intent = AgentIntent(upstream_agents=["classifier"])
        plan = generate_plan(intent)
        assert plan.manifest.get("task_protocol") is True
        assert plan.manifest.get("receives_tasks_from") == ["classifier"]

    def test_review_flag_propagates(self):
        intent = AgentIntent(requires_review=True)
        plan = generate_plan(intent)
        assert plan.manifest.get("review_workflow") is True

    def test_instruction_skeleton_has_sections(self):
        intent = AgentIntent(
            purpose="Test agent",
            upstream_agents=["feeder"],
            downstream_agents=["consumer"],
        )
        plan = generate_plan(intent)
        assert "Your Role" in plan.instruction_skeleton
        assert "Tasks" in plan.instruction_skeleton
        assert "Output" in plan.instruction_skeleton
        assert "Task Protocol" in plan.instruction_skeleton


class TestPlanToScaffold:
    def test_writes_files(self, tmp_path):
        intent = AgentIntent(purpose="Test scaffolding")
        plan = generate_plan(intent)

        # Create required directories
        (tmp_path / "docs" / "agents").mkdir(parents=True)
        (tmp_path / "brain" / "agents").mkdir(parents=True)

        result = plan_to_scaffold(plan, "test-scaffold", "Test Scaffold", repo_root=tmp_path)
        assert "manifest" in result
        assert "instruction" in result

        manifest_path = tmp_path / "docs" / "agents" / "test-scaffold.yaml"
        assert manifest_path.exists()

        instr_path = tmp_path / "brain" / "agents" / "TEST_SCAFFOLD.md"
        assert instr_path.exists()

        content = instr_path.read_text()
        assert "Test Scaffold" in content
