"""
Config validation tests for Steps 8/10/11 of the Open Source plan.

TDD: These tests define the desired final state.
- Step 8: openclaw.json agent registration (6 agents, RBAC, heartbeat)
- Step 10: jobs.json cron reassignment (proper agent IDs, no disabled jobs)
- Step 11: 7 skills in .agents/skills/
"""

import json
import os
import re
from pathlib import Path

import pytest

# ── Paths ──────────────────────────────────────────────────────────────
OPENCLAW_DIR = Path(os.path.expanduser("~/.openclaw"))
OPENCLAW_JSON = OPENCLAW_DIR / "openclaw.json"
JOBS_JSON = OPENCLAW_DIR / "cron" / "jobs.json"
WORKSPACE = Path(os.path.expanduser("~/clawd"))
SKILLS_DIR = WORKSPACE / ".agents" / "skills"

EXPECTED_AGENT_IDS = {"main", "supervisor", "email", "calendar", "crm", "vision"}
EXPECTED_SKILLS = {
    "send-email",
    "crm-lookup",
    "escalate",
    "health-check",
    "run-pipeline",
    "vision-analyze",
    "memory-search",
}

# CRM write tools that restricted agents should NOT have
CRM_WRITE_TOOLS = {
    "create_person",
    "update_person",
    "delete_person",
    "merge_contacts",
    "merge_companies",
    "create_note",
    "update_note",
    "delete_note",
    "create_task",
    "update_task",
    "delete_task",
}


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def openclaw_config():
    assert OPENCLAW_JSON.exists(), f"{OPENCLAW_JSON} not found"
    return json.loads(OPENCLAW_JSON.read_text())


@pytest.fixture(scope="module")
def agents_list(openclaw_config):
    return openclaw_config.get("agents", {}).get("list", [])


@pytest.fixture(scope="module")
def agents_by_id(agents_list):
    return {a["id"]: a for a in agents_list}


@pytest.fixture(scope="module")
def agents_defaults(openclaw_config):
    return openclaw_config.get("agents", {}).get("defaults", {})


@pytest.fixture(scope="module")
def jobs_config():
    assert JOBS_JSON.exists(), f"{JOBS_JSON} not found"
    return json.loads(JOBS_JSON.read_text())


@pytest.fixture(scope="module")
def jobs_list(jobs_config):
    return jobs_config.get("jobs", [])


# ═══════════════════════════════════════════════════════════════════════
# Step 8: openclaw.json — Agent Registration
# ═══════════════════════════════════════════════════════════════════════


class TestOpenClawJson:
    def test_parseable(self, openclaw_config):
        """openclaw.json is valid JSON."""
        assert isinstance(openclaw_config, dict)

    def test_agents_list_has_6_agents(self, agents_list):
        """Exactly 6 agents registered."""
        assert len(agents_list) == 6, (
            f"Expected 6 agents, got {len(agents_list)}: "
            f"{[a.get('id') for a in agents_list]}"
        )

    def test_agent_ids_correct(self, agents_by_id):
        """All expected agent IDs present."""
        assert set(agents_by_id.keys()) == EXPECTED_AGENT_IDS

    def test_main_is_default(self, agents_by_id):
        """Main agent has default=true."""
        assert agents_by_id["main"].get("default") is True

    def test_no_triage_agent(self, agents_by_id):
        """Dead triage agent is removed."""
        assert "triage" not in agents_by_id

    def test_heartbeat_disabled_globally(self, agents_defaults):
        """Default heartbeat is disabled (every=0)."""
        hb = agents_defaults.get("heartbeat", {})
        assert hb.get("every") == "0", (
            f"Default heartbeat should be '0', got '{hb.get('every')}'"
        )

    def test_only_supervisor_has_heartbeat(self, agents_by_id):
        """Only supervisor agent has a heartbeat enabled."""
        for agent_id, agent in agents_by_id.items():
            hb = agent.get("heartbeat", {})
            hb_every = hb.get("every", "0")
            if agent_id == "supervisor":
                assert hb_every != "0", "Supervisor should have heartbeat enabled"
            else:
                # Either no heartbeat key (inherits disabled default) or explicitly "0"
                if "heartbeat" in agent:
                    assert hb_every == "0", (
                        f"Agent '{agent_id}' should not have heartbeat, got '{hb_every}'"
                    )

    def test_supervisor_heartbeat_is_17m(self, agents_by_id):
        """Supervisor heartbeat is 17 minutes."""
        hb = agents_by_id["supervisor"].get("heartbeat", {})
        assert hb.get("every") == "17m"

    def test_supervisor_denies_crm_writes(self, agents_by_id):
        """Supervisor cannot perform CRM write operations."""
        denied = set(agents_by_id["supervisor"].get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), (
            f"Supervisor missing CRM write denials: {CRM_WRITE_TOOLS - denied}"
        )

    def test_calendar_denies_crm_and_web(self, agents_by_id):
        """Calendar agent cannot do CRM writes, messaging, or web ops."""
        denied = set(agents_by_id["calendar"].get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), (
            f"Calendar missing CRM write denials: {CRM_WRITE_TOOLS - denied}"
        )
        assert "message" in denied
        assert "web_search" in denied
        assert "web_fetch" in denied
        assert "sessions_spawn" in denied

    def test_vision_denies_crm_and_web(self, agents_by_id):
        """Vision agent cannot do CRM writes, messaging, or web ops."""
        denied = set(agents_by_id["vision"].get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), (
            f"Vision missing CRM write denials: {CRM_WRITE_TOOLS - denied}"
        )
        assert "message" in denied
        assert "web_search" in denied
        assert "web_fetch" in denied
        assert "sessions_spawn" in denied

    def test_main_has_no_tool_restrictions(self, agents_by_id):
        """Main agent has no tool deny list."""
        tools = agents_by_id["main"].get("tools", {})
        deny = tools.get("deny", [])
        assert len(deny) == 0, f"Main should have no restrictions, got deny: {deny}"

    def test_email_denies_message(self, agents_by_id):
        """Email agent cannot send messages (uses gog CLI instead)."""
        denied = set(agents_by_id["email"].get("tools", {}).get("deny", []))
        assert "message" in denied
        assert "sessions_spawn" in denied

    def test_crm_denies_message(self, agents_by_id):
        """CRM agent cannot send messages."""
        denied = set(agents_by_id["crm"].get("tools", {}).get("deny", []))
        assert "message" in denied


# ═══════════════════════════════════════════════════════════════════════
# Step 10: jobs.json — Cron Job Reassignment
# ═══════════════════════════════════════════════════════════════════════


class TestJobsJson:
    def test_parseable(self, jobs_config):
        """jobs.json is valid JSON."""
        assert isinstance(jobs_config, dict)
        assert "jobs" in jobs_config

    def test_no_disabled_jobs_remain(self, jobs_list):
        """All disabled/one-shot jobs removed."""
        for job in jobs_list:
            assert job.get("enabled", True) is True, (
                f"Disabled job should be removed: {job.get('name')}"
            )

    def test_no_delete_after_run_jobs(self, jobs_list):
        """No one-shot (deleteAfterRun) jobs remain."""
        for job in jobs_list:
            assert not job.get("deleteAfterRun", False), (
                f"One-shot job should be removed: {job.get('name')}"
            )

    def test_email_jobs_use_email_agent(self, jobs_list):
        """Email Classifier, Analyst, Responder use 'email' agent."""
        email_job_names = {"Email Classifier", "Email Analyst", "Email Responder"}
        for job in jobs_list:
            if job["name"] in email_job_names:
                assert job["agentId"] == "email", (
                    f"Job '{job['name']}' should use 'email' agent, got '{job['agentId']}'"
                )

    def test_supervisor_job_uses_supervisor(self, jobs_list):
        """Supervisor Heartbeat uses 'supervisor' agent."""
        for job in jobs_list:
            if job["name"] == "Supervisor Heartbeat":
                assert job["agentId"] == "supervisor", (
                    f"Supervisor job should use 'supervisor' agent, got '{job['agentId']}'"
                )
                return
        pytest.fail("Supervisor Heartbeat job not found")

    def test_calendar_job_uses_calendar_agent(self, jobs_list):
        """Calendar Monitor uses 'calendar' agent."""
        for job in jobs_list:
            if job["name"] == "Calendar Monitor":
                assert job["agentId"] == "calendar", (
                    f"Calendar job should use 'calendar' agent, got '{job['agentId']}'"
                )
                return
        pytest.fail("Calendar Monitor job not found")

    def test_vision_job_uses_vision_agent(self, jobs_list):
        """Vision Monitor uses 'vision' agent."""
        for job in jobs_list:
            if job["name"] == "Vision Monitor":
                assert job["agentId"] == "vision", (
                    f"Vision job should use 'vision' agent, got '{job['agentId']}'"
                )
                return
        pytest.fail("Vision Monitor job not found")

    def test_crm_jobs_use_crm_agent(self, jobs_list):
        """Conversation Inbox/Resolver and CRM Steward use 'crm' agent."""
        crm_job_names = {
            "Conversation Inbox Monitor",
            "Conversation Resolver",
            "CRM Steward",
        }
        for job in jobs_list:
            if job["name"] in crm_job_names:
                assert job["agentId"] == "crm", (
                    f"Job '{job['name']}' should use 'crm' agent, got '{job['agentId']}'"
                )

    def test_briefing_jobs_use_main(self, jobs_list):
        """Morning Briefing and Evening Wind-Down use 'main' agent."""
        briefing_names = {"Morning Briefing", "Evening Wind-Down"}
        for job in jobs_list:
            if job["name"] in briefing_names:
                assert job["agentId"] == "main", (
                    f"Job '{job['name']}' should use 'main' agent, got '{job['agentId']}'"
                )

    def test_briefing_errors_reset(self, jobs_list):
        """Briefing jobs have consecutiveErrors reset to 0."""
        briefing_names = {"Morning Briefing", "Evening Wind-Down"}
        for job in jobs_list:
            if job["name"] in briefing_names:
                errors = job.get("state", {}).get("consecutiveErrors", 0)
                assert errors == 0, (
                    f"Job '{job['name']}' should have 0 consecutiveErrors, got {errors}"
                )

    def test_all_jobs_use_registered_agents(self, jobs_list):
        """Every job references a registered agent ID."""
        for job in jobs_list:
            assert job["agentId"] in EXPECTED_AGENT_IDS, (
                f"Job '{job['name']}' uses unregistered agent '{job['agentId']}'"
            )


# ═══════════════════════════════════════════════════════════════════════
# Step 11: Skills
# ═══════════════════════════════════════════════════════════════════════


class TestSkills:
    def test_skills_directory_exists(self):
        """Skills directory exists at workspace/.agents/skills/."""
        assert SKILLS_DIR.is_dir(), f"{SKILLS_DIR} does not exist"

    def test_all_7_skills_have_skill_md(self):
        """Each skill has a SKILL.md file."""
        for skill_name in EXPECTED_SKILLS:
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            assert skill_file.is_file(), (
                f"Missing SKILL.md for skill '{skill_name}' at {skill_file}"
            )

    def test_skill_frontmatter_has_name_and_description(self):
        """Each SKILL.md has valid YAML frontmatter with name and description."""
        for skill_name in EXPECTED_SKILLS:
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            if not skill_file.exists():
                pytest.skip(f"Skill {skill_name} not created yet")
            content = skill_file.read_text()
            assert content.startswith("---"), (
                f"Skill '{skill_name}' SKILL.md must start with YAML frontmatter"
            )
            # Extract frontmatter
            parts = content.split("---", 2)
            assert len(parts) >= 3, (
                f"Skill '{skill_name}' SKILL.md has malformed frontmatter"
            )
            frontmatter = parts[1]
            assert "name:" in frontmatter, (
                f"Skill '{skill_name}' missing 'name' in frontmatter"
            )
            assert "description:" in frontmatter, (
                f"Skill '{skill_name}' missing 'description' in frontmatter"
            )

    def test_skill_names_are_kebab_case(self):
        """All skill directory names use kebab-case."""
        if not SKILLS_DIR.is_dir():
            pytest.skip("Skills directory not created yet")
        for entry in SKILLS_DIR.iterdir():
            if entry.is_dir():
                assert re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", entry.name), (
                    f"Skill directory '{entry.name}' is not kebab-case"
                )

    def test_skill_names_match_frontmatter(self):
        """Skill directory name matches the 'name' field in frontmatter."""
        for skill_name in EXPECTED_SKILLS:
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            if not skill_file.exists():
                pytest.skip(f"Skill {skill_name} not created yet")
            content = skill_file.read_text()
            parts = content.split("---", 2)
            if len(parts) < 3:
                continue
            frontmatter = parts[1]
            for line in frontmatter.strip().splitlines():
                if line.strip().startswith("name:"):
                    fm_name = line.split(":", 1)[1].strip().strip("\"'")
                    assert fm_name == skill_name, (
                        f"Skill '{skill_name}' frontmatter name is '{fm_name}'"
                    )
                    break
