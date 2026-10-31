# Instructions for coding agents

`CLAUDE.md` is the canonical instruction file for every agent working in this
repository, not only Claude Code. This file only points at it, so the two can
never disagree. Where a rule there says "Claude", it means you.

Before you change anything, read in full:

1. `CLAUDE.md` — the platform/instance boundary, the public-repository rule,
   the numbered rules, the ticket-first and test-first workflow, quality gates,
   reviews, Git conventions and releases.
2. `.claude/rules/tdd.md` and `.claude/rules/commands.md`. Claude Code loads
   these automatically; you have to open them yourself.
3. The `CLAUDE.md` of the subsystem you work in: `robothor/engine/CLAUDE.md`,
   `crm/CLAUDE.md`.
4. `brain/CLAUDE.md`, if it exists (instance-local and gitignored).

Add new rules to `CLAUDE.md` or `.claude/rules/`, never here.

## GitHub Actions runners

- Never use `ubuntu-latest`; the `runner-labels` check fails on it (RD-2286).
- New workflows: `runs-on: ubuntu-26.04`.
- Changing an existing workflow: move all its jobs from `ubuntu-24.04` to `ubuntu-26.04` in the same PR and get CI green. Deploy/release workflows move only after the repo's CI is green on 26.04.
