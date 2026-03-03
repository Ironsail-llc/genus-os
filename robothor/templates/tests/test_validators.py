"""Tests for template validation."""

from robothor.templates.validators import (
    validate_bundle,
    validate_setup_yaml,
    validate_skill_md,
    validate_template_resolves,
)


class TestValidateSkillMd:
    def test_valid_skill_md(self, tmp_bundle):
        errors = validate_skill_md(tmp_bundle)
        assert all(e.level != "error" for e in errors)

    def test_missing_skill_md(self, tmp_path):
        bundle = tmp_path / "empty"
        bundle.mkdir()
        errors = validate_skill_md(bundle)
        assert len(errors) == 1
        assert errors[0].level == "warning"

    def test_missing_frontmatter(self, tmp_path):
        bundle = tmp_path / "bad"
        bundle.mkdir()
        (bundle / "SKILL.md").write_text("# No frontmatter\nJust text.")
        errors = validate_skill_md(bundle)
        assert any(e.level == "error" for e in errors)

    def test_missing_required_field(self, tmp_path):
        bundle = tmp_path / "incomplete"
        bundle.mkdir()
        (bundle / "SKILL.md").write_text("---\nname: Test\n---\n# Test\n")
        errors = validate_skill_md(bundle)
        # Missing version, description, format
        error_fields = [e.message for e in errors if e.level == "error"]
        assert len(error_fields) >= 2

    def test_invalid_yaml_frontmatter(self, tmp_path):
        bundle = tmp_path / "broken"
        bundle.mkdir()
        (bundle / "SKILL.md").write_text("---\n: invalid: yaml: :\n---\n")
        errors = validate_skill_md(bundle)
        assert any("Invalid YAML" in e.message for e in errors)


class TestValidateSetupYaml:
    def test_valid_setup(self, tmp_bundle):
        errors = validate_setup_yaml(tmp_bundle)
        assert all(e.level != "error" for e in errors)

    def test_missing_setup(self, tmp_path):
        bundle = tmp_path / "no-setup"
        bundle.mkdir()
        errors = validate_setup_yaml(bundle)
        assert any(e.level == "error" for e in errors)

    def test_no_variables(self, tmp_path):
        bundle = tmp_path / "no-vars"
        bundle.mkdir()
        (bundle / "setup.yaml").write_text("agent_id: test\n")
        errors = validate_setup_yaml(bundle)
        assert any(e.level == "warning" for e in errors)


class TestValidateTemplateResolves:
    def test_resolves_cleanly(self, tmp_bundle):
        errors = validate_template_resolves(tmp_bundle)
        # May have unresolved "version" since no context is provided,
        # but no hard errors
        hard_errors = [e for e in errors if e.level == "error"]
        assert len(hard_errors) == 0

    def test_missing_template(self, tmp_path):
        bundle = tmp_path / "no-template"
        bundle.mkdir()
        (bundle / "setup.yaml").write_text("agent_id: test\nvariables: {}")
        errors = validate_template_resolves(bundle)
        assert any("not found" in e.message for e in errors)

    def test_invalid_yaml_after_resolve(self, tmp_path):
        bundle = tmp_path / "bad-yaml"
        bundle.mkdir()
        (bundle / "setup.yaml").write_text("agent_id: test\nvariables: {}")
        (bundle / "manifest.template.yaml").write_text("key: [invalid yaml\n")
        errors = validate_template_resolves(bundle)
        assert any("invalid" in e.message.lower() for e in errors)


class TestValidateBundle:
    def test_valid_bundle(self, tmp_bundle):
        errors = validate_bundle(tmp_bundle)
        hard_errors = [e for e in errors if e.level == "error"]
        assert len(hard_errors) == 0

    def test_bundle_collects_all_errors(self, tmp_path):
        """A completely empty directory should produce multiple errors."""
        bundle = tmp_path / "empty"
        bundle.mkdir()
        errors = validate_bundle(bundle)
        assert len(errors) >= 2  # At minimum: SKILL.md warning + setup.yaml error
