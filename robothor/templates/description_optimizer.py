"""Hub-ready metadata optimization for agent template bundles.

Ensures agents produce quality SKILL.md and programmatic.json for
programmaticresources.com and agent hub discovery.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DescriptionScore:
    """Score breakdown for an agent description."""

    specificity: float = 0.0  # 0-1: mentions concrete data/actions
    actionability: float = 0.0  # 0-1: uses active verbs
    searchability: float = 0.0  # 0-1: includes searchable keywords
    length_score: float = 0.0  # 0-1: within ideal word range
    total: float = 0.0  # 0-100: weighted composite

    @property
    def grade(self) -> str:
        if self.total >= 80:
            return "A"
        if self.total >= 60:
            return "B"
        if self.total >= 40:
            return "C"
        return "D"


@dataclass
class HubReadinessReport:
    """Hub readiness assessment for a template bundle."""

    score: int = 0  # 0-100
    breakdown: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


# Active verbs that indicate actionability
ACTIVE_VERBS = {
    "classifies",
    "routes",
    "analyzes",
    "monitors",
    "detects",
    "generates",
    "creates",
    "validates",
    "checks",
    "processes",
    "extracts",
    "transforms",
    "sends",
    "receives",
    "schedules",
    "drafts",
    "reviews",
    "compares",
    "filters",
    "aggregates",
    "summarizes",
    "triages",
    "escalates",
    "enriches",
    "syncs",
    "fetches",
    "publishes",
    "notifies",
    "reconciles",
    "audits",
}

# Concrete nouns that indicate specificity
CONCRETE_NOUNS = {
    "email",
    "emails",
    "inbox",
    "calendar",
    "task",
    "tasks",
    "contact",
    "contacts",
    "message",
    "messages",
    "url",
    "urls",
    "link",
    "links",
    "file",
    "files",
    "pr",
    "pull request",
    "commit",
    "code",
    "api",
    "webhook",
    "database",
    "notification",
    "report",
    "alert",
    "metric",
    "log",
    "event",
    "person",
    "company",
    "pipeline",
    "queue",
    "status",
}

# Tool category to tag mapping
TOOL_TAG_MAP = {
    "git_status": "git",
    "git_diff": "git",
    "git_branch": "git",
    "git_commit": "git",
    "git_push": "git",
    "create_pull_request": "git",
    "web_fetch": "web",
    "web_search": "web",
    "create_task": "task-management",
    "update_task": "task-management",
    "resolve_task": "task-management",
    "list_my_tasks": "task-management",
    "search_memory": "memory",
    "store_memory": "memory",
    "get_entity": "memory",
    "create_message": "communication",
    "log_interaction": "communication",
    "make_call": "voice",
    "look": "vision",
    "who_is_here": "vision",
    "enroll_face": "vision",
    "create_person": "crm",
    "list_people": "crm",
    "create_company": "crm",
    "spawn_agent": "orchestration",
    "spawn_agents": "orchestration",
}


def analyze_description(description: str) -> DescriptionScore:
    """Score a description on specificity, actionability, searchability, and length."""
    score = DescriptionScore()
    words = description.lower().split()
    word_count = len(words)

    if word_count == 0:
        return score

    # Specificity: proportion of concrete nouns
    concrete_found = sum(1 for w in words if w.strip(".,;:()") in CONCRETE_NOUNS)
    # Also check multi-word concrete nouns
    desc_lower = description.lower()
    for noun in CONCRETE_NOUNS:
        if " " in noun and noun in desc_lower:
            concrete_found += 1
    score.specificity = min(1.0, concrete_found / max(3, word_count * 0.2))

    # Actionability: presence of active verbs
    active_found = sum(1 for w in words if w.strip(".,;:()") in ACTIVE_VERBS)
    score.actionability = min(1.0, active_found / max(1, min(3, word_count // 10 + 1)))

    # Searchability: has keywords someone would search for
    searchable_keywords = CONCRETE_NOUNS | ACTIVE_VERBS
    keyword_found = sum(1 for w in words if w.strip(".,;:()") in searchable_keywords)
    score.searchability = min(1.0, keyword_found / max(3, word_count * 0.15))

    # Length: 10-80 words ideal
    if 10 <= word_count <= 80:
        score.length_score = 1.0
    elif word_count < 10:
        score.length_score = word_count / 10.0
    else:
        score.length_score = max(0.0, 1.0 - (word_count - 80) / 80.0)

    # Composite (0-100)
    score.total = (
        score.specificity * 30
        + score.actionability * 30
        + score.searchability * 20
        + score.length_score * 20
    )

    return score


def suggest_tags(manifest: dict, instruction_content: str = "") -> list[str]:
    """Suggest tags based on tools, produced/consumed tags, and instruction keywords."""
    tags: set[str] = set()

    # From tools_allowed categories
    for tool in manifest.get("tools_allowed", []):
        tag = TOOL_TAG_MAP.get(tool)
        if tag:
            tags.add(tag)

    # From tags_produced and tags_consumed
    tags.update(manifest.get("tags_produced", []))
    tags.update(manifest.get("tags_consumed", []))

    # From department
    dept = manifest.get("department", "")
    if dept and dept != "custom":
        tags.add(dept)

    # From instruction keywords
    if instruction_content:
        content_lower = instruction_content.lower()
        keyword_tag_map = {
            "email": "email",
            "calendar": "calendar",
            "github": "git",
            "pull request": "git",
            "vision": "vision",
            "camera": "vision",
            "voice": "voice",
            "phone": "voice",
            "security": "security",
            "monitor": "monitoring",
            "alert": "alerting",
            "report": "reporting",
            "brief": "briefing",
        }
        for keyword, tag in keyword_tag_map.items():
            if keyword in content_lower:
                tags.add(tag)

    return sorted(tags)


def generate_skill_md(manifest: dict, instruction_content: str = "") -> str:
    """Generate a rich SKILL.md with frontmatter, description, variables, and capabilities."""
    agent_id = manifest.get("id", "unknown")
    name = manifest.get("name", agent_id)
    version = manifest.get("version", "0.0.0")
    description = manifest.get("description", "")
    department = manifest.get("department", "custom")

    tags = suggest_tags(manifest, instruction_content)

    # Frontmatter
    frontmatter = {
        "name": name,
        "version": version,
        "description": description,
        "format": "robothor-native/v1",
        "department": department,
        "tags": tags,
    }

    lines = ["---"]
    lines.append(yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip())
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    lines.append(description)
    lines.append("")

    # Capabilities section
    capabilities = []
    if manifest.get("task_protocol"):
        capabilities.append("Processes tasks from its CRM queue")
    if manifest.get("creates_tasks_for"):
        targets = ", ".join(manifest["creates_tasks_for"])
        capabilities.append(f"Routes work to downstream agents: {targets}")
    if manifest.get("hooks"):
        streams = [h.get("stream", "") for h in manifest["hooks"] if isinstance(h, dict)]
        capabilities.append(f"Triggers on events: {', '.join(streams)}")
    if manifest.get("review_workflow"):
        capabilities.append("Supports human-in-the-loop review workflow")

    tools = manifest.get("tools_allowed", [])
    if "web_fetch" in tools or "web_search" in tools:
        capabilities.append("Can fetch and search the web")
    if "spawn_agent" in tools or "spawn_agents" in tools:
        capabilities.append("Can delegate work to sub-agents")
    if any(t.startswith("git_") for t in tools) or "create_pull_request" in tools:
        capabilities.append("Can interact with git repositories")

    if capabilities:
        lines.append("## Capabilities")
        lines.append("")
        for cap in capabilities:
            lines.append(f"- {cap}")
        lines.append("")

    # Coordination section
    coordination = []
    if manifest.get("receives_tasks_from"):
        coordination.append(f"Receives tasks from: {', '.join(manifest['receives_tasks_from'])}")
    if manifest.get("creates_tasks_for"):
        coordination.append(f"Creates tasks for: {', '.join(manifest['creates_tasks_for'])}")
    if manifest.get("reports_to"):
        coordination.append(f"Reports to: {manifest['reports_to']}")

    if coordination:
        lines.append("## Coordination")
        lines.append("")
        for item in coordination:
            lines.append(f"- {item}")
        lines.append("")

    # Model tier
    model = manifest.get("model", {}).get("primary", "")
    if model:
        lines.append("## Model")
        lines.append("")
        lines.append(f"Primary: `{model}`")
        lines.append("")

    return "\n".join(lines)


def score_hub_readiness(bundle_path: str | Path) -> HubReadinessReport:
    """Score a template bundle's readiness for hub publishing (0-100)."""
    bundle = Path(bundle_path)
    report = HubReadinessReport()
    report.breakdown = {}

    # 1. SKILL.md exists with valid frontmatter (20 pts)
    skill_path = bundle / "SKILL.md"
    skill_score = 0
    if skill_path.exists():
        content = skill_path.read_text()
        frontmatter_match = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
        if frontmatter_match:
            try:
                fm = yaml.safe_load(frontmatter_match.group(1)) or {}
                required = ["name", "version", "description", "format"]
                present = sum(1 for f in required if fm.get(f))
                skill_score = (present / len(required)) * 20
            except yaml.YAMLError:
                report.issues.append("SKILL.md has invalid YAML frontmatter")
        else:
            report.issues.append("SKILL.md missing YAML frontmatter")
    else:
        report.issues.append("SKILL.md not found")
        report.suggestions.append(
            "Generate SKILL.md with description_optimizer.generate_skill_md()"
        )
    report.breakdown["skill_md"] = int(skill_score)

    # 2. programmatic.json complete (20 pts)
    prog_path = bundle / "programmatic.json"
    prog_score = 0
    if prog_path.exists():
        try:
            prog = json.loads(prog_path.read_text())
            required = ["name", "id", "version", "format", "description"]
            present = sum(1 for f in required if prog.get(f))
            prog_score = (present / len(required)) * 20
        except (json.JSONDecodeError, OSError):
            report.issues.append("programmatic.json is invalid JSON")
    else:
        report.issues.append("programmatic.json not found")
    report.breakdown["programmatic_json"] = int(prog_score)

    # 3. Description score > 70 (20 pts)
    desc_score_pts = 0
    description = ""
    if skill_path.exists():
        content = skill_path.read_text()
        fm_match = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
        if fm_match:
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
                description = fm.get("description", "")
            except yaml.YAMLError:
                pass
    if description:
        desc_analysis = analyze_description(description)
        if desc_analysis.total >= 70:
            desc_score_pts = 20
        else:
            desc_score_pts = int((desc_analysis.total / 70) * 20)
            report.suggestions.append(
                f"Description scores {desc_analysis.total:.0f}/100 — aim for 70+"
            )
    else:
        report.issues.append("No description found for scoring")
    report.breakdown["description_quality"] = desc_score_pts

    # 4. 3+ relevant tags (10 pts)
    tag_score = 0
    tags = []
    if prog_path.exists():
        try:
            prog = json.loads(prog_path.read_text())
            tags = prog.get("tags", [])
        except (json.JSONDecodeError, OSError):
            pass
    if not tags and skill_path.exists():
        content = skill_path.read_text()
        fm_match = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
        if fm_match:
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
                tags = fm.get("tags", [])
            except yaml.YAMLError:
                pass
    if len(tags) >= 3:
        tag_score = 10
    elif tags:
        tag_score = int((len(tags) / 3) * 10)
        report.suggestions.append(f"Only {len(tags)} tag(s) — aim for 3+")
    else:
        report.suggestions.append("No tags found — add tags for discoverability")
    report.breakdown["tags"] = tag_score

    # 5. setup.yaml has typed variables with descriptions (15 pts)
    setup_path = bundle / "setup.yaml"
    setup_score = 0
    if setup_path.exists():
        try:
            setup = yaml.safe_load(setup_path.read_text()) or {}
            variables = setup.get("variables", {})
            if variables:
                typed_count = 0
                described_count = 0
                total = len(variables)
                for _var_name, var_def in variables.items():
                    if isinstance(var_def, dict):
                        if var_def.get("type"):
                            typed_count += 1
                        if var_def.get("description"):
                            described_count += 1
                    # Simple key: value counts as neither
                if total > 0:
                    setup_score = int((typed_count / total) * 7.5 + (described_count / total) * 7.5)
            else:
                setup_score = 5  # No variables is ok, just less points
                report.suggestions.append(
                    "setup.yaml has no variables — consider adding customization points"
                )
        except (yaml.YAMLError, OSError):
            report.issues.append("setup.yaml is invalid")
    else:
        report.issues.append("setup.yaml not found")
    report.breakdown["setup_yaml"] = setup_score

    # 6. instructions.template.md uses {{ }} for customizable values (15 pts)
    instr_path = bundle / "instructions.template.md"
    instr_score = 0
    if instr_path.exists():
        content = instr_path.read_text()
        template_vars = re.findall(r"\{\{.*?\}\}", content)
        if template_vars:
            instr_score = min(15, len(template_vars) * 5)
        else:
            instr_score = 5  # File exists but no template vars
            report.suggestions.append(
                "instructions.template.md has no {{ }} variables — "
                "consider templating agent name, timezone, etc."
            )
    else:
        report.issues.append("instructions.template.md not found")
    report.breakdown["instructions_template"] = instr_score

    report.score = sum(report.breakdown.values())
    return report
