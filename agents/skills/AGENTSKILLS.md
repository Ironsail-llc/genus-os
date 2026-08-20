# Genus OS Skills — agentskills.io-compatible format

Skills in `agents/skills/<name>/` follow the [agentskills.io](https://agentskills.io)
frontmatter convention so they're portable across the broader ecosystem
(Hermes Agent, Claude marketplace, lobehub, skills.sh, etc).

## Canonical layout

```
agents/skills/<kebab-name>/
  SKILL.md          # YAML frontmatter + markdown body (this file)
  meta.json         # static, tracked metadata — created_by, write_origin, …
  state.json        # gitignored runtime telemetry — usage_count, last_used
  references/       # optional: long-form supporting notes
  templates/        # optional: starter files to copy + edit
  scripts/          # optional: deterministic helper scripts
```

## SKILL.md frontmatter keys

| Key | Required | Notes |
|---|---|---|
| `name` | yes | kebab-case slug, 3–60 chars, matches directory name |
| `description` | yes | one-line summary (≤100 chars — truncated in the system-prompt catalog) |
| `tags` | no | list of strings; surfaced in `list_skills` and `skill_view` |
| `parameters` | no | list of `{name, type, description, required, default?}` |
| `tools_required` | no | list of tool names the body assumes are available |
| `trigger_phrases` | no | hint strings the lean catalog may surface to help the model match intent |
| `output_format` | no | default `"text"` |

## Rip 3 system-prompt catalog vs on-demand load

When `ROBOTHOR_RIP_3_ENABLED=1`:

1. The system prompt carries only `- /<name> — <truncated-description>`
   per skill. No per-skill bodies, no parameter signatures, no
   trigger lists. Catalog stays small even with hundreds of skills.
2. The agent calls `skill_view(name="<name>")` to load any one body
   on demand. Each call bumps the skill's usage counter.
3. `invoke_skill(name=..., args=...)` runs the loaded skill.

When the rip is off, the legacy verbose catalog (description +
signature + triggers per skill) is rendered — preserves backwards
compatibility while Rip 3 rolls out.

## meta.json (static metadata) + state.json (runtime telemetry)

meta.json is tracked in git and stays byte-stable at runtime. All mutable
telemetry lives in a gitignored `state.json` sidecar next to it, and the
lifecycle `state` is derived on the fly (never persisted). Read skills
through `robothor.engine.skills.read_skill_view` — it merges both files
(sidecar wins per key) and adds the derived `state`.

state.json (gitignored, written atomically):

| Key | Set by | Used for |
|---|---|---|
| `usage_count` | `skill_view`, `invoke_skill` | Curator ranks stale skills |
| `last_used` | `skill_view`, `invoke_skill` | Lifecycle staleness anchor |

meta.json (tracked, static):

| Key | Set by | Used for |
|---|---|---|
| `created_by` | `create_skill` | Audit trail |
| `write_origin` | `create_skill` (Rip 4) | `"foreground"` or `"background_review"` |
| `is_agent_created` | `create_skill` (Rip 4) | `True` only for background-review-fork writes; curator only touches these |
| `auto_generated` | `create_skill` | Legacy flag preserved for older tooling |
| `content_hash` | `create_skill` | Drift detection for skill bodies |
| `pinned` | operator (manual) | Curator skips pinned skills entirely |

## Authoring discipline (Rip 2 guardrail)

When `ROBOTHOR_RIP_2_ENABLED=1`, the create/update handlers reject
one-off session-artifact names: `fix-*`, `debug-*`, `audit-*`, names
embedding PR numbers (`-pr-123`), day-of-week suffixes, single
library names (`pandas`, `requests`), or all-digit slugs.

Skills should describe a **class of work** durable across sessions
(`database-migrations`, `api-client-debugging`, `crash-recovery`),
not a one-off task.
