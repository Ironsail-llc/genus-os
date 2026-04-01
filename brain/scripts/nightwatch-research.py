#!/usr/bin/env python3
"""
DEPRECATED — replaced by brain/scripts/nightwatch.py (consolidated nightwatch agent).
This file is kept for git history. Use nightwatch.py instead.

---
Nightwatch Self-Improving — Phase A: Research.

Runs weekly (Sunday 1 AM). Uses Claude Code with web search to analyze
competitor agent frameworks, compare against our engine, and create
CRM tasks for actionable improvements.

Usage:
    python brain/scripts/nightwatch-research.py           # Normal run
    python brain/scripts/nightwatch-research.py --dry-run  # Show what would be researched
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brain.scripts.nightwatch_lib import (
    REPO_ROOT,
    create_nightwatch_task,
    extract_pr_url,
    invoke_claude_code,
    read_memory_block,
    setup_logging,
    today_str,
    write_memory_block,
    write_status_file,
)

log = setup_logging("nightwatch-research")

RESEARCH_BUDGET = 1.00

RESEARCH_SYSTEM_PROMPT = """\
You are a senior AI engineer researching agent framework patterns. You have read-only
access to the Robothor codebase and web search capabilities.

Rules:
1. Read our engine code first to understand current capabilities.
2. Use web search to find the latest features in competitor frameworks.
3. Be specific — cite URLs, version numbers, and concrete features.
4. Focus on practical improvements, not theoretical patterns.
5. Output your analysis as a JSON object with this schema:
   {
     "improvements": [
       {
         "title": "Feature name",
         "description": "What it does and why it matters",
         "frameworks": ["Which frameworks have it"],
         "impact": "high/medium/low",
         "effort": "high/medium/low",
         "files_affected": ["list of files that would change"],
         "rationale": "Why we should adopt this"
       }
     ],
     "summary": "Overall findings summary"
   }
6. Limit to 5-7 improvements, ranked by impact/effort ratio.
7. Do NOT make any file changes. This is research only.
"""

RESEARCH_PROMPT = """\
Research the latest features and patterns in these agent frameworks:
- LangChain / LangGraph (latest stable)
- CrewAI (latest stable)
- AutoGen / AG2 (Microsoft, latest)
- OpenAI Agents SDK
- Anthropic Agent SDK / Claude Code SDK

Then read our engine codebase to understand current capabilities:
- robothor/engine/ — the core agent engine
- robothor/engine/runner.py — LLM loop
- robothor/engine/tools.py — tool registry
- robothor/engine/planner.py — planning
- robothor/engine/scratchpad.py — working memory
- robothor/engine/escalation.py — error recovery
- robothor/engine/guardrails.py — safety rails
- robothor/engine/checkpoint.py — mid-run checkpoints
- robothor/engine/verifier.py — self-validation
- robothor/engine/telemetry.py — observability
- robothor/engine/analytics.py — fleet analytics

Compare and identify improvements we're missing. Focus on:
1. Tool calling patterns (parallel tools, streaming, retries)
2. Memory and context management (long-term, episodic, semantic)
3. Multi-agent orchestration (delegation, communication, consensus)
4. Error recovery and self-healing patterns
5. Observability and debugging (traces, replays, cost tracking)
6. Structured output and response validation

Output your analysis as the JSON schema described in the system prompt.
"""

RESEARCH_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Bash(find:*)"


def parse_improvements(result: dict) -> list[dict]:
    """Extract improvement proposals from Claude Code output."""
    # Try to find JSON in the output
    text = result.get("result", "") if result.get("raw_output") else json.dumps(result)

    # Look for the improvements array
    try:
        if isinstance(result, dict) and "improvements" in result:
            return result["improvements"]
    except (TypeError, KeyError):
        pass

    # Try to parse JSON from text
    import re
    match = re.search(r'\{[^{}]*"improvements"\s*:\s*\[.*?\]\s*[,}]', text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group() if match.group().endswith("}") else match.group() + "}")
            return parsed.get("improvements", [])
        except json.JSONDecodeError:
            pass

    # Try to find any JSON array
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group())
            if items and isinstance(items[0], dict) and "title" in items[0]:
                return items
        except (json.JSONDecodeError, IndexError, TypeError):
            pass

    log.warning("Could not parse structured improvements from Claude output")
    return []


def main(dry_run: bool = False) -> None:
    log.info("Nightwatch research starting (dry_run=%s)", dry_run)

    if dry_run:
        log.info("[DRY RUN] Would research: LangChain, CrewAI, AutoGen, OpenAI Agents SDK, Anthropic SDK")
        log.info("[DRY RUN] Would compare against robothor/engine/ codebase")
        log.info("[DRY RUN] Budget: $%.2f", RESEARCH_BUDGET)
        return

    # Invoke Claude Code for research (read-only, no worktree needed)
    result = invoke_claude_code(
        cwd=REPO_ROOT,
        prompt=RESEARCH_PROMPT,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        allowed_tools=RESEARCH_TOOLS,
        budget=RESEARCH_BUDGET,
        timeout=900,  # 15 min for research
    )

    if result.get("error"):
        log.error("Research failed: %s", result["error"])
        write_status_file("brain/memory/nightwatch-research-status.md", (
            f"# Nightwatch Research Status\n\n"
            f"Last run: {datetime.now().isoformat()}\n"
            f"Status: FAILED\n"
            f"Error: {result['error']}\n"
        ))
        return

    # Parse improvements
    improvements = parse_improvements(result)
    log.info("Found %d improvement proposals", len(improvements))

    # Create CRM tasks for actionable improvements (up to 5)
    tasks_created = []
    for imp in improvements[:5]:
        title = imp.get("title", "Unnamed improvement")
        impact = imp.get("impact", "medium")
        effort = imp.get("effort", "medium")

        # Map impact/effort to priority
        priority_map = {
            ("high", "low"): "high",
            ("high", "medium"): "high",
            ("medium", "low"): "normal",
            ("high", "high"): "normal",
            ("medium", "medium"): "normal",
            ("low", "low"): "normal",
            ("medium", "high"): "low",
            ("low", "medium"): "low",
            ("low", "high"): "low",
        }
        priority = priority_map.get((impact, effort), "normal")

        body = (
            f"## {title}\n\n"
            f"**Impact:** {impact} | **Effort:** {effort}\n"
            f"**Frameworks:** {', '.join(imp.get('frameworks', []))}\n\n"
            f"### Description\n{imp.get('description', 'No description')}\n\n"
            f"### Rationale\n{imp.get('rationale', 'No rationale')}\n\n"
            f"### Files Affected\n"
            + "\n".join(f"- `{f}`" for f in imp.get("files_affected", []))
            + "\n\n---\nGenerated by Nightwatch Research"
        )

        task_id = create_nightwatch_task(
            title=f"[Feature] {title}",
            body=body,
            tags=["nightwatch", "feature"],
            priority=priority,
        )
        if task_id:
            tasks_created.append({"title": title, "task_id": task_id, "priority": priority})
            log.info("Created task: %s (priority=%s)", title, priority)

    # Write full research report
    date = today_str()
    summary = result.get("summary", "") if isinstance(result, dict) else ""
    report = (
        f"# Nightwatch Research Report — {date}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Improvements Identified: {len(improvements)}\n"
        f"## Tasks Created: {len(tasks_created)}\n\n"
    )
    for i, imp in enumerate(improvements, 1):
        report += (
            f"### {i}. {imp.get('title', 'Unknown')}\n"
            f"- Impact: {imp.get('impact', '?')} | Effort: {imp.get('effort', '?')}\n"
            f"- Frameworks: {', '.join(imp.get('frameworks', []))}\n"
            f"- {imp.get('description', '')}\n\n"
        )

    write_status_file("brain/memory/nightwatch-research.md", report)

    # Write status
    write_status_file("brain/memory/nightwatch-research-status.md", (
        f"# Nightwatch Research Status\n\n"
        f"Last run: {datetime.now().isoformat()}\n"
        f"Improvements found: {len(improvements)}\n"
        f"Tasks created: {len(tasks_created)}\n"
    ))

    # Update nightwatch_log
    nightwatch_log = read_memory_block("nightwatch_log")
    log_entry = f"[{date}] research: {len(improvements)} improvements found, {len(tasks_created)} tasks created"
    updated_log = nightwatch_log.rstrip() + "\n" + log_entry + "\n"
    lines = updated_log.strip().split("\n")
    if len(lines) > 100:
        lines = lines[-100:]
    write_memory_block("nightwatch_log", "\n".join(lines))

    log.info("Nightwatch research complete: %d improvements, %d tasks", len(improvements), len(tasks_created))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightwatch Research")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be researched")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
