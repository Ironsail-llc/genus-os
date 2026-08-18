---
name: Auto Researcher
version: 2026-03-31
description: Iterative optimization agent using the autoresearch pattern -- define a metric, iterate on changes, measure, keep or revert, accumulate learnings
format: robothor-native/v1
department: operations
---

# Auto Researcher

An optimization agent that turns any measurable business metric into a self-improving experiment. Based on Andrej Karpathy's autoresearch pattern.

## What It Does

Given a metric command (any shell command that outputs a number) and a search space (files the agent can modify), the Auto Researcher:

1. Measures the baseline
2. Hypothesizes an improvement
3. Makes a focused change
4. Measures the result
5. Keeps improvements, reverts failures
6. Records learnings for the next iteration
7. Repeats until convergence or budget exhaustion

## Use Cases

- Optimize agent fleet success rates by tuning instruction files
- Improve email reply rates by iterating on classifier rules
- Reduce agent costs by adjusting model selections and iteration limits
- Any business metric with a numeric output and configurable inputs

## Requirements

- Experiment tools must be available in the engine (`experiment_create`, `experiment_measure`, `experiment_commit`, `experiment_status`)
- A metric command that outputs a single number
- Files in the search space that the agent is allowed to modify

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `model_primary` | `openrouter/anthropic/claude-sonnet-4.6` | Primary LLM model |
| `timezone` | `UTC` | Schedule timezone |
| `delivery_mode` | `announce` | How to notify on significant improvements |
| `cost_budget_usd` | `3.0` | Max cost per agent run |
