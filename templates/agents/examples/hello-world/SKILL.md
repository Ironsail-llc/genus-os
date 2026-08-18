---
name: Hello World
version: "2026-03-17"
description: Minimal example agent — reads a file, writes status, done
format: robothor-native/v1
department: examples
---

# Hello World

A minimal example agent for learning the Genus OS agent system. Reads a file,
writes a status update, and exits. Use this as a starting point for building
your own agents.

## Variables

- **model_primary**: Primary LLM model (default: `ollama_chat/qwen3.5:122b`)
- **cron_expr**: Cron schedule (default: `0 12 * * *` — noon daily)
- **timezone**: Schedule timezone (default: `UTC`)

## Dependencies

None — this is a standalone agent.
