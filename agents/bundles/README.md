# Skill bundles (Rip 11)

YAML aliases that load several skills together via one `/bundle-name`
slash command. Mirrors the Hermes pattern (`agent/skill_bundles.py`).

## Format

```yaml
# agents/bundles/release.yaml
name: release          # kebab-case, 3–60 chars, must be unique
description: ship a release end-to-end       # one line
instruction: |         # optional preamble shown above the bundled bodies
  Follow each linked skill in order; do not skip safety steps.
skills:                # required, non-empty
  - code-review
  - run-tests
  - update-changelog
  - open-pr
```

## Lookup precedence (matches Hermes)

1. `/<name>` matches a bundle in `agents/bundles/` → load all referenced skills.
2. Else `/<name>` matches a skill in `agents/skills/` → existing `invoke_skill` path.
3. Else: unknown, falls through to normal message handling.

Bundle wins on collision so an operator can override a single skill with
a multi-step bundle of the same name.
