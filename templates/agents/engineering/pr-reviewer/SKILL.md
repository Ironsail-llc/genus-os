---
name: PR Reviewer
version: "2026-10-05"
description: Reviews pull requests with Claude Code and posts the result on GitHub
format: robothor-native/v1
department: engineering
---

# PR Reviewer

Reviews pull requests from configured GitHub repositories, pull requests that
request the bot's review, and pull-request links posted in a Google Chat
space. Each review is a read-only Claude Code job guided by the `pr-review`
skill; the verdict is recomputed in code from the findings before anything is
posted. Results are announced in the Chat thread and, optionally, as a
Telegram digest.

## Variables

- **delivery_mode**: `none` (default) or `announce` for the Telegram digest
- **reports_to** / **escalates_to**: supervisor agent (default: `main`)

## Dependencies

- `templates/workflows/pr-review-intake.yaml` and `pr-review-run.yaml`,
  installed into the instance's `docs/workflows/`
- `ROBOTHOR_PR_REVIEW_*` settings (repositories, Chat space, bot login)
- `robothor claude-code login` and `GITHUB_TOKEN` in the vault
- The `pr-review` skill

See docs/PR_REVIEWER.md.
