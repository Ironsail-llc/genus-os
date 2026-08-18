---
name: Concierge
version: 2026-03-04
description: Self-configuring agent that detects usage patterns and proposes new agents
format: robothor-native/v1
department: core
---

# Concierge

Self-configuring agent that analyzes fleet activity, detects repeating usage patterns, and proposes new agents to automate recurring work.

## What It Does

- Runs daily at 8 PM
- Gathers fleet analytics (run stats, anomalies, error patterns)
- Detects patterns: repeated tool sequences, time-of-day clustering, topic frequency
- Proposes new agents via CRM tasks (max 1 per run, assigned to main agent)
- Tracks observations in the `concierge_observations` memory block
- Respects rejections with a 30-day cooldown

## Variables

None — uses global defaults.
