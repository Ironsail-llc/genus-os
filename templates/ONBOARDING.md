# Onboarding & Agent Builder — How It Works

This document explains the CLAUDE.md template system that powers Genus OS's onboarding experience.

## What Are These Templates?

Genus OS ships with two CLAUDE.md templates that turn any Claude Code session into a specialized guide:

| Template | File | Purpose |
|----------|------|---------|
| **Onboarding** | `templates/CLAUDE.md` | Guides new users through setup — from `robothor init` to a running fleet |
| **Agent Builder** | `templates/AGENT_BUILDER.md` | Teaches the unit-agent-in-workflows pattern: focused agents composed into pipelines via CRM tasks, event hooks, and workflow YAML |

These are **project instruction files** — when placed at the root of a workspace or in `.claude/`, Claude Code reads them automatically and adjusts its behavior.

## How They Get Activated

### During `robothor init`

When a user runs `robothor init`, the setup wizard copies both templates to the workspace root:

```
workspace/
├── CLAUDE.md              ← Onboarding guide (from templates/CLAUDE.md)
├── AGENT_BUILDER.md       ← Agent builder reference (from templates/AGENT_BUILDER.md)
├── brain/
├── docs/
└── .env
```

Template variables (`{{ai_name}}`, `{{owner_name}}`) are resolved with the values entered during init.

### Manual Activation

Users can also activate templates manually:

```bash
# Copy to project root (affects all Claude Code sessions in this directory)
cp templates/CLAUDE.md ./CLAUDE.md

# Or copy to .claude/ (affects only Claude Code sessions using this profile)
cp templates/AGENT_BUILDER.md .claude/AGENT_BUILDER.md
```

## How They Interact

The two templates have a natural handoff:

1. **Onboarding CLAUDE.md** guides the user through initial setup (init → identity → agents → engine start)
2. Once everything is running, it mentions the Agent Builder for when they want custom agents
3. **Agent Builder CLAUDE.md** is a deep reference for the unit-agent-in-workflows paradigm — model tiering, CRM coordination, orchestration patterns, template packaging, and complete pipeline examples

Users typically:
- Start with the Onboarding guide (automatic after init)
- Graduate to normal usage (the onboarding guide becomes passive)
- Pull in the Agent Builder when they want to create custom agents

## The Concierge Agent

The `concierge` agent template (in `templates/agents/core/concierge/`) is a self-configuring agent that watches usage patterns and proposes new agents:

- Runs daily at 8 PM
- Analyzes: tool usage patterns, time-of-day clustering, topic frequency
- Proposes new agents via CRM tasks (max 1 per run)
- Tracks proposals in the `concierge_observations` memory block
- Respects rejections with a 30-day cooldown

It's included in the **minimal** preset so even bare-bones installations get the self-improvement loop.

## Customization

### Editing the Onboarding Guide

The onboarding CLAUDE.md is designed to be disposable. Once setup is complete, users can:
- Delete it (they don't need onboarding anymore)
- Replace it with their own project-level CLAUDE.md
- Keep it (it gracefully falls through to "normal assistant mode" when everything is configured)

### Editing the Agent Builder

The Agent Builder teaches the unit-agent-in-workflows pattern. Customize it by:
- Adding your own orchestration patterns and pipeline examples
- Adjusting model tier mappings in `_defaults.yaml` for your budget
- Removing tool categories you don't use
- Adding domain-specific unit agent templates

### Template Variables

Both templates support these variables (resolved during `robothor init`):

| Variable | Default | Source |
|----------|---------|--------|
| `{{ai_name}}` | Robothor | User input during init |
| `{{owner_name}}` | (empty) | User input during init |

Add custom variables by editing `robothor/setup.py`'s template resolution loop.
