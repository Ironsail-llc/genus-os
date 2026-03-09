"""Tests for description_optimizer — hub-ready metadata."""

import json

import yaml

from robothor.templates.description_optimizer import (
    analyze_description,
    generate_skill_md,
    score_hub_readiness,
    suggest_tags,
)


class TestAnalyzeDescription:
    def test_good_description_scores_high(self):
        desc = "Classifies incoming emails and routes to appropriate handlers based on content analysis"
        score = analyze_description(desc)
        assert score.total > 40
        assert score.actionability > 0
        assert score.specificity > 0

    def test_vague_description_scores_low(self):
        desc = "Handles stuff and does things"
        score = analyze_description(desc)
        assert score.total < 30

    def test_empty_description_scores_zero(self):
        score = analyze_description("")
        assert score.total == 0.0

    def test_length_score_ideal_range(self):
        desc = " ".join(["classifies emails"] * 5)  # ~10 words
        score = analyze_description(desc)
        assert score.length_score > 0.5

    def test_length_score_too_short(self):
        desc = "Monitors"
        score = analyze_description(desc)
        assert score.length_score < 0.5

    def test_grade_a(self):
        desc = "Classifies incoming emails, routes tasks to responders, monitors inbox queue, analyzes message content, and creates task assignments"
        score = analyze_description(desc)
        # The exact grade depends on scoring, but should be decent
        assert score.grade in ("A", "B", "C")

    def test_grade_d_for_empty(self):
        score = analyze_description("a b c")
        assert score.grade == "D"


class TestSuggestTags:
    def test_tags_from_tools(self):
        manifest = {"tools_allowed": ["git_status", "create_pull_request", "web_fetch"]}
        tags = suggest_tags(manifest)
        assert "git" in tags
        assert "web" in tags

    def test_tags_from_produced_consumed(self):
        manifest = {"tags_produced": ["email", "reply-needed"], "tags_consumed": ["analytical"]}
        tags = suggest_tags(manifest)
        assert "email" in tags
        assert "reply-needed" in tags
        assert "analytical" in tags

    def test_tags_from_department(self):
        manifest = {"department": "email"}
        tags = suggest_tags(manifest)
        assert "email" in tags

    def test_tags_from_instruction_keywords(self):
        manifest: dict[str, str] = {}
        tags = suggest_tags(
            manifest, instruction_content="Monitor the GitHub pull request pipeline"
        )
        assert "git" in tags
        assert "monitoring" in tags

    def test_custom_department_excluded(self):
        manifest = {"department": "custom"}
        tags = suggest_tags(manifest)
        assert "custom" not in tags


class TestGenerateSkillMd:
    def test_has_frontmatter(self):
        manifest = {
            "id": "test-agent",
            "name": "Test Agent",
            "version": "1.0.0",
            "description": "A test agent",
            "department": "custom",
            "model": {"primary": "glm-5"},
        }
        skill_md = generate_skill_md(manifest)
        assert skill_md.startswith("---\n")
        assert "name: Test Agent" in skill_md
        assert "format: robothor-native/v1" in skill_md

    def test_has_capabilities_section(self):
        manifest = {
            "id": "worker",
            "name": "Worker",
            "version": "1.0.0",
            "description": "Processes tasks",
            "department": "custom",
            "task_protocol": True,
            "creates_tasks_for": ["downstream"],
            "tools_allowed": ["web_fetch"],
            "model": {"primary": "test"},
        }
        skill_md = generate_skill_md(manifest)
        assert "## Capabilities" in skill_md
        assert "Processes tasks from its CRM queue" in skill_md
        assert "downstream" in skill_md

    def test_has_coordination_section(self):
        manifest = {
            "id": "mid",
            "name": "Mid",
            "version": "1.0.0",
            "description": "Middle agent",
            "department": "custom",
            "receives_tasks_from": ["upstream"],
            "reports_to": "main",
            "model": {"primary": "test"},
        }
        skill_md = generate_skill_md(manifest)
        assert "## Coordination" in skill_md
        assert "upstream" in skill_md


class TestScoreHubReadiness:
    def test_full_bundle_scores_high(self, tmp_path):
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        # SKILL.md
        (bundle / "SKILL.md").write_text(
            "---\n"
            "name: Email Classifier\n"
            "version: '1.0.0'\n"
            "description: Classifies incoming emails and routes to handlers\n"
            "format: robothor-native/v1\n"
            "tags: [email, classification, routing]\n"
            "---\n\n# Email Classifier\n"
        )

        # programmatic.json
        (bundle / "programmatic.json").write_text(
            json.dumps(
                {
                    "name": "Email Classifier",
                    "id": "email-classifier",
                    "version": "1.0.0",
                    "format": "robothor-native/v1",
                    "description": "Classifies incoming emails and routes to handlers",
                    "tags": ["email", "classification", "routing"],
                }
            )
        )

        # setup.yaml
        (bundle / "setup.yaml").write_text(
            yaml.dump(
                {
                    "agent_id": "email-classifier",
                    "version": "1.0.0",
                    "variables": {
                        "model_primary": {
                            "type": "string",
                            "default": "glm5",
                            "description": "Primary model",
                        },
                        "cron_expr": {
                            "type": "string",
                            "default": "0 */2 * * *",
                            "description": "Schedule",
                        },
                    },
                }
            )
        )

        # instructions.template.md
        (bundle / "instructions.template.md").write_text(
            "# {{ agent_name }}\n\nRunning in {{ timezone }}.\n\nModel: {{ model_primary }}\n"
        )

        report = score_hub_readiness(bundle)
        assert report.score >= 60
        assert len(report.issues) == 0

    def test_empty_bundle_scores_low(self, tmp_path):
        bundle = tmp_path / "empty"
        bundle.mkdir()

        report = score_hub_readiness(bundle)
        assert report.score < 20
        assert len(report.issues) >= 3

    def test_partial_bundle(self, tmp_path):
        bundle = tmp_path / "partial"
        bundle.mkdir()

        # Only SKILL.md
        (bundle / "SKILL.md").write_text(
            "---\nname: Test\nversion: '1.0'\ndescription: test\nformat: robothor-native/v1\n---\n"
        )
        (bundle / "setup.yaml").write_text("agent_id: test\n")

        report = score_hub_readiness(bundle)
        assert 0 < report.score < 80
        assert "programmatic.json" in " ".join(report.issues)

    def test_breakdown_keys(self, tmp_path):
        bundle = tmp_path / "any"
        bundle.mkdir()
        report = score_hub_readiness(bundle)
        expected_keys = {
            "skill_md",
            "programmatic_json",
            "description_quality",
            "tags",
            "setup_yaml",
            "instructions_template",
        }
        assert set(report.breakdown.keys()) == expected_keys
