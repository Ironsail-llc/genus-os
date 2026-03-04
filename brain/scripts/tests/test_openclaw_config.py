"""
Config validation tests for OpenClaw agent registration (step 8),
cron job reassignment (step 10), and skills (step 11).

TDD — these define the target state. Written before implementation.
"""

import json
import os
import re

import pytest

# Paths
OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
JOBS_JSON = os.path.expanduser("~/.openclaw/cron/jobs.json")
SKILLS_DIR = os.path.expanduser("~/clawd/.agents/skills")

# Expected agent IDs after step 8
EXPECTED_AGENTS = {"main", "supervisor", "email", "calendar", "crm", "vision"}

# CRM write tools that supervisor/calendar/vision should NOT have
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

# Expected skills after step 11
EXPECTED_SKILLS = {
    "send-email",
    "crm-lookup",
    "escalate",
    "health-check",
    "run-pipeline",
    "vision-analyze",
    "memory-search",
}


@pytest.fixture
def openclaw():
    with open(OPENCLAW_JSON) as f:
        return json.load(f)


@pytest.fixture
def agents(openclaw):
    return {a["id"]: a for a in openclaw["agents"]["list"]}


@pytest.fixture
def jobs():
    with open(JOBS_JSON) as f:
        return json.load(f)


@pytest.fixture
def enabled_jobs(jobs):
    return [j for j in jobs["jobs"] if j.get("enabled", False)]


# ==================== openclaw.json tests (step 8) ====================


class TestOpenClawParseable:
    def test_openclaw_json_parseable(self, openclaw):
        assert isinstance(openclaw, dict)
        assert "agents" in openclaw

    def test_agents_list_has_6_agents(self, agents):
        assert len(agents) == 6, f"Expected 6 agents, got {len(agents)}: {list(agents.keys())}"

    def test_agent_ids_correct(self, agents):
        assert set(agents.keys()) == EXPECTED_AGENTS

    def test_main_is_default(self, agents):
        assert agents["main"].get("default") is True

    def test_only_main_has_default(self, agents):
        defaults = [aid for aid, a in agents.items() if a.get("default")]
        assert defaults == ["main"]


class TestHeartbeat:
    def test_defaults_heartbeat_disabled(self, openclaw):
        defaults = openclaw["agents"]["defaults"]
        hb = defaults.get("heartbeat", {})
        assert hb.get("every") == "0", "Default heartbeat should be disabled (every: '0')"

    def test_only_supervisor_has_heartbeat(self, agents):
        for aid, agent in agents.items():
            hb = agent.get("heartbeat", {})
            if aid == "supervisor":
                assert hb.get("every") == "17m", "Supervisor should have 17m heartbeat"
            else:
                # Either no heartbeat key, or inherits disabled default
                if "heartbeat" in agent:
                    every = agent["heartbeat"].get("every", "0")
                    # main can have heartbeat too (for interactive sessions)
                    if aid != "main":
                        assert every == "0", f"Agent {aid} should not have heartbeat enabled"


class TestToolScoping:
    def test_supervisor_denies_crm_writes(self, agents):
        sup = agents["supervisor"]
        denied = set(sup.get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), (
            f"Supervisor missing CRM write denials: {CRM_WRITE_TOOLS - denied}"
        )

    def test_calendar_denies_crm_and_web(self, agents):
        cal = agents["calendar"]
        denied = set(cal.get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), "Calendar missing CRM denials"
        assert "message" in denied, "Calendar should deny message"
        assert "web_search" in denied, "Calendar should deny web_search"
        assert "web_fetch" in denied, "Calendar should deny web_fetch"
        assert "sessions_spawn" in denied, "Calendar should deny sessions_spawn"

    def test_vision_denies_crm_and_web(self, agents):
        vis = agents["vision"]
        denied = set(vis.get("tools", {}).get("deny", []))
        assert CRM_WRITE_TOOLS.issubset(denied), "Vision missing CRM denials"
        assert "message" in denied, "Vision should deny message"
        assert "web_search" in denied, "Vision should deny web_search"
        assert "web_fetch" in denied, "Vision should deny web_fetch"
        assert "sessions_spawn" in denied, "Vision should deny sessions_spawn"

    def test_main_has_no_tool_restrictions(self, agents):
        main = agents["main"]
        assert "tools" not in main or not main.get("tools", {}).get("deny"), (
            "Main agent should have no tool restrictions"
        )

    def test_email_denies_message(self, agents):
        email = agents["email"]
        denied = set(email.get("tools", {}).get("deny", []))
        assert "message" in denied, "Email should deny message tool"
        assert "sessions_spawn" in denied, "Email should deny sessions_spawn"

    def test_crm_denies_message(self, agents):
        crm = agents["crm"]
        denied = set(crm.get("tools", {}).get("deny", []))
        assert "message" in denied, "CRM should deny message tool"


class TestNoTriageAgent:
    def test_triage_agent_removed(self, agents):
        assert "triage" not in agents, "Dead triage agent should be removed"


# ==================== jobs.json tests (step 10) ====================


class TestJobsParseable:
    def test_jobs_json_parseable(self, jobs):
        assert isinstance(jobs, dict)
        assert "jobs" in jobs

    def test_no_disabled_jobs_remain(self, jobs):
        disabled = [j for j in jobs["jobs"] if not j.get("enabled", False)]
        assert len(disabled) == 0, (
            f"Found {len(disabled)} disabled jobs that should be removed: "
            f"{[j['name'] for j in disabled]}"
        )


class TestJobAgentAssignment:
    def test_email_jobs_use_email_agent(self, enabled_jobs):
        email_names = {"Email Classifier", "Email Analyst", "Email Responder"}
        for job in enabled_jobs:
            if job["name"] in email_names:
                assert job["agentId"] == "email", (
                    f"Job '{job['name']}' should use agent 'email', got '{job['agentId']}'"
                )

    def test_supervisor_job_uses_supervisor(self, enabled_jobs):
        for job in enabled_jobs:
            if job["name"] == "Supervisor Heartbeat":
                assert job["agentId"] == "supervisor", (
                    f"Supervisor Heartbeat should use agent 'supervisor', got '{job['agentId']}'"
                )
                return
        pytest.fail("Supervisor Heartbeat job not found")

    def test_calendar_job_uses_calendar_agent(self, enabled_jobs):
        for job in enabled_jobs:
            if job["name"] == "Calendar Monitor":
                assert job["agentId"] == "calendar", (
                    f"Calendar Monitor should use agent 'calendar', got '{job['agentId']}'"
                )
                return
        pytest.fail("Calendar Monitor job not found")

    def test_vision_job_uses_vision_agent(self, enabled_jobs):
        for job in enabled_jobs:
            if job["name"] == "Vision Monitor":
                assert job["agentId"] == "vision", (
                    f"Vision Monitor should use agent 'vision', got '{job['agentId']}'"
                )
                return
        pytest.fail("Vision Monitor job not found")

    def test_crm_jobs_use_crm_agent(self, enabled_jobs):
        crm_names = {"Conversation Inbox Monitor", "Conversation Resolver", "CRM Steward"}
        for job in enabled_jobs:
            if job["name"] in crm_names:
                assert job["agentId"] == "crm", (
                    f"Job '{job['name']}' should use agent 'crm', got '{job['agentId']}'"
                )

    def test_briefing_jobs_use_main(self, enabled_jobs):
        briefing_names = {"Morning Briefing", "Evening Wind-Down"}
        for job in enabled_jobs:
            if job["name"] in briefing_names:
                assert job["agentId"] == "main", (
                    f"Job '{job['name']}' should use agent 'main', got '{job['agentId']}'"
                )

    def test_briefing_errors_reset(self, enabled_jobs):
        briefing_names = {"Morning Briefing", "Evening Wind-Down"}
        for job in enabled_jobs:
            if job["name"] in briefing_names:
                errors = job.get("state", {}).get("consecutiveErrors", 0)
                assert errors == 0, (
                    f"Job '{job['name']}' should have consecutiveErrors=0, got {errors}"
                )

    def test_all_jobs_reference_valid_agents(self, enabled_jobs):
        for job in enabled_jobs:
            assert job["agentId"] in EXPECTED_AGENTS, (
                f"Job '{job['name']}' references unknown agent '{job['agentId']}'"
            )


# ==================== skills tests (step 11) ====================


class TestSkills:
    def test_skills_directory_exists(self):
        assert os.path.isdir(SKILLS_DIR), f"Skills directory not found: {SKILLS_DIR}"

    def test_all_7_skills_exist(self):
        existing = set(os.listdir(SKILLS_DIR))
        for skill in EXPECTED_SKILLS:
            assert skill in existing, f"Skill directory missing: {skill}"

    def test_all_7_skills_have_skill_md(self):
        for skill in EXPECTED_SKILLS:
            skill_md = os.path.join(SKILLS_DIR, skill, "SKILL.md")
            assert os.path.isfile(skill_md), f"Missing SKILL.md in {skill}"

    def test_skill_frontmatter_has_name_and_description(self):
        for skill in EXPECTED_SKILLS:
            skill_md = os.path.join(SKILLS_DIR, skill, "SKILL.md")
            with open(skill_md) as f:
                content = f.read()
            # Check YAML frontmatter
            assert content.startswith("---"), f"{skill}/SKILL.md missing frontmatter"
            # Extract frontmatter
            parts = content.split("---", 2)
            assert len(parts) >= 3, f"{skill}/SKILL.md malformed frontmatter"
            fm = parts[1]
            assert "name:" in fm, f"{skill}/SKILL.md missing 'name' in frontmatter"
            assert "description:" in fm, f"{skill}/SKILL.md missing 'description' in frontmatter"

    def test_skill_names_are_kebab_case(self):
        for skill in EXPECTED_SKILLS:
            assert re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", skill), (
                f"Skill name '{skill}' is not kebab-case"
            )

    def test_skill_frontmatter_name_matches_directory(self):
        for skill in EXPECTED_SKILLS:
            skill_md = os.path.join(SKILLS_DIR, skill, "SKILL.md")
            with open(skill_md) as f:
                content = f.read()
            parts = content.split("---", 2)
            fm = parts[1]
            for line in fm.strip().split("\n"):
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                    assert name == skill, (
                        f"Skill {skill}: frontmatter name '{name}' doesn't match directory"
                    )
