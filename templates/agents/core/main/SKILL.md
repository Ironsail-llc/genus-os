---
name: Main
version: 2026-08-18
description: Primary interactive agent — talks to the operator and delegates work to the fleet
format: robothor-native/v1
department: core
---

# Main

The primary interactive agent. Handles direct requests, delegates moderate-to-complex
work to unit agents via `spawn_agent`/`spawn_agents`, and coordinates the fleet through
CRM tasks.

## What It Does

- Default point of contact for the operator (persistent session).
- Delegates focused work to unit agents instead of doing everything itself.
- Checks and resolves its own task queue every turn.
- Tracks open threads in `brain/memory/main-status.md`.

## Variables

None — uses global defaults (`model_primary`, `timezone`) from `_defaults.yaml`.

## After Install

`delivery.mode` ships as `none`. Configure a real channel (`announce` + `channel`/`to`)
once the instance has one wired up, and rewrite the instruction file with the operator's
identity and the fleet that's actually installed.
