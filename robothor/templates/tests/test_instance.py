"""Tests for instance configuration management."""

from robothor.templates.instance import InstanceConfig


class TestInstanceConfig:
    def test_load_creates_directories(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        assert instance.base_dir.exists()
        assert instance.overrides_dir.exists()

    def test_exists_false_initially(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        assert instance.exists is False

    def test_init_config(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        config = instance.init_config(
            timezone="Europe/London",
            owner_name="Test User",
        )
        assert instance.exists is True
        assert config["instance"]["timezone"] == "Europe/London"
        assert config["instance"]["owner_name"] == "Test User"

    def test_config_roundtrip(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        data = {"instance": {"timezone": "UTC"}, "defaults": {"model": "test"}}
        instance.config = data
        loaded = instance.config
        assert loaded["instance"]["timezone"] == "UTC"
        assert loaded["defaults"]["model"] == "test"

    def test_record_install(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        instance.record_install(
            agent_id="test-agent",
            source="local",
            source_path="/tmp/test",
            version="1.0.0",
            variables={"model": "gpt-4"},
            manifest_path="docs/agents/test-agent.yaml",
            instruction_path="brain/TEST.md",
        )
        agents = instance.installed_agents
        assert "test-agent" in agents
        assert agents["test-agent"]["version"] == "1.0.0"
        assert agents["test-agent"]["source"] == "local"

    def test_record_remove(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        instance.record_install(
            agent_id="test-agent",
            source="local",
            source_path="/tmp/test",
            version="1.0.0",
            variables={},
            manifest_path="docs/agents/test-agent.yaml",
            instruction_path="brain/TEST.md",
        )
        record = instance.record_remove("test-agent")
        assert record is not None
        assert record["version"] == "1.0.0"
        assert "test-agent" not in instance.installed_agents

    def test_record_remove_nonexistent(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        record = instance.record_remove("nonexistent")
        assert record is None

    def test_agent_overrides(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)

        # Initially empty
        assert instance.get_agent_overrides("test-agent") == {}

        # Save overrides
        instance.save_agent_overrides("test-agent", {"model_primary": "custom-model"})
        overrides = instance.get_agent_overrides("test-agent")
        assert overrides["model_primary"] == "custom-model"

    def test_archive_agent(self, tmp_instance_dir, tmp_path):
        instance = InstanceConfig.load(tmp_instance_dir)

        # Create a fake manifest file
        manifest = tmp_path / "test.yaml"
        manifest.write_text("id: test-agent")

        archive_path = instance.archive_agent("test-agent", {"manifest": manifest})
        assert archive_path.exists()
        assert (archive_path / "test.yaml").exists()

    def test_multiple_agents(self, tmp_instance_dir):
        instance = InstanceConfig.load(tmp_instance_dir)
        for i in range(3):
            instance.record_install(
                agent_id=f"agent-{i}",
                source="local",
                source_path=f"/tmp/agent-{i}",
                version="1.0.0",
                variables={},
                manifest_path=f"docs/agents/agent-{i}.yaml",
                instruction_path=f"brain/AGENT_{i}.md",
            )
        agents = instance.installed_agents
        assert len(agents) == 3
        assert "agent-0" in agents
        assert "agent-2" in agents
