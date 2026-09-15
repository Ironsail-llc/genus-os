# 4. Agents, skills and plugins

Goal: a fleet you chose, on schedules you chose, built from parts whose
provenance you can name.

## Three fields, in the browser

The fastest honest way to create an agent is the Helm's agent builder. Name,
description and department are the three fields; everything else has a default
you can change later, and the advanced drawer is where you change it now:

| Advanced field | What it decides |
|---|---|
| `model.primary` and `model.fallbacks` | Which model runs it, and what it falls back to. An ordered list, replaced wholesale when you edit it |
| `schedule` | Cron, heartbeat or worker. `schedule.enabled: false` is the stop switch |
| `delivery` | Which channel the agent reports on, and to whom. `none` is right for most workers |
| `tools_allowed` / `tools_denied` | The agent's tool surface. Start narrow |
| `instruction_file` | The markdown file that is the agent's actual instructions |

The builder scaffolds, validates, writes both files and calls the engine's
reconcile in one operation. A write answers with a `reconcile` block;
`applied: false` means the file is on disk and the engine has not picked it up
yet — the watchdog will, within five minutes.

**An edit is refused only for the errors it introduced.** Every edit is
validated twice, as the document was and as the edit leaves it, and the
difference decides the refusal. "A save must not break a manifest" and "a save
is gated on the manifest being unbroken" are different promises, and the second
locks you out exactly when you need in: an agent with a bad cron could
otherwise neither be repaired nor disabled, and disable is the stop control. A
pre-existing fault comes back in `pre_existing`, so "allowed through" never
reads as "blessed".

## Manifests are the source of truth

The browser is a convenience over files. What the engine runs is
`<workspace>/docs/agents/<id>.yaml` and the instruction file it names — both
instance data, both gitignored, both surviving every platform upgrade.

```bash
genus agent list
genus agent catalog
genus agent scaffold reporter --description "Weekly rollup for the leadership team"
genus agent install --preset standard
```

`minimal` is three agents with almost nothing scheduled; `standard` adds email
triage, calendar watch and briefings; `full` is the whole catalogue. The wizard
installs one of these, and so can you afterwards — the same code path either
way.

A hand edit still needs a reconcile before the new schedule fires: the Helm's
route does it for you, and the scheduler watchdog does it within five minutes
regardless. Validate before you wait:

```bash
python scripts/validate_agents.py --agent reporter
```

The manifest schema, the instruction-file contract, the model-tiering strategy
and worked multi-agent examples are in the
[Agent Builder reference](../AGENT_BUILDER.md).

## Schedules and run truth

An agent can be triggered three ways — cron, the heartbeat, or a worker queue —
and `schedule.enabled: false` silences all three at once while keeping the
manifest, the instructions and the schedule row. That is what lets the fleet
view show "off" rather than making a silenced agent look deleted.

Three different questions get three different answers, and confusing them is
the classic operational mistake:

| Question | Where it is answered |
|---|---|
| Did it **run**? | The run row exists |
| Did it **deliver**? | `delivery_status` on that run |
| Did it **complete** the objective? | The goal or task the run was for |

A run that errored still ran. A run that delivered still may have achieved
nothing. [Observability](../OBSERVABILITY.md) has the full distinction and the
Helm's Automations view, which shows one card per scheduled agent with its last
result.

## Skills

A skill is a markdown instruction bundle an agent can load on demand: the
procedure for one recurring job, written once, in the agent's own workspace.
Skills travel with the agent when you export it. Keep them small and specific —
a skill that tries to cover three jobs is one an agent applies to the wrong
one.

## Sharing an agent as a bundle

An agent is portable. `genus agent export` writes a bundle carrying the
manifest, the instruction file, every skill it references, and a `bundle.yaml`
naming what the agent *requires*: plugins, adapters, secrets and skills, each
file with its sha256.

```bash
genus agent export reporter --out ./reporter.tar.gz
genus agent install ./reporter.tar.gz
genus agent install https://example.com/reporter.tar.gz --sha256 abcdef --yes
genus agent install ./reporter.tar.gz --id reporter-eu --strict
```

Two properties make this safe enough to send to another company:

- **A credential literal anywhere in an export is a hard failure.** Not a
  warning — the export exits non-zero, names the file and line, and writes
  nothing. Adapter values are collapsed to `${NAME}` and listed under
  `requires.secrets`, so the recipient is told what to supply rather than
  handed what you had.
- **Install previews before it installs.** You see the plan — what it needs,
  what is missing, what it will write — and `--yes` is the separate decision.
  `--strict` refuses unless every requirement is already satisfied.

A URL install requires `--sha256`. The Helm offers the same export as a Share
button. Bundle format and the `requires` check are in the
[Agent Builder reference](../AGENT_BUILDER.md#8a-sharing-an-agent).

## Plugins

Everything that is not the deterministic core is a plugin: tools, models,
scheduled jobs, operator commands, sandboxes, channels, named services. In an
enterprise, three mechanisms matter more than the extension points.

**The registry is signed.** An index is Ed25519-signed over canonical JSON,
publisher keys are pinned in the platform or supplied per instance, and a
tampered byte, an unpinned key, a stale or future-dated index, a redirect off
its origin, or a name published by two publishers are each refused with a
sentence rather than a stack trace.

**The installer looks before it installs.** The wheel is streamed under a byte
cap and checked against its sha256 before anything opens it; the archive is read
under bounds — member count, total size, no links, path containment, no
duplicates; the manifest must match the index. A static scan then produces a
verdict:

| Verdict | What happens |
|---|---|
| `safe` | Installs |
| `review` | Installs only with your explicit acceptance of that exact plan |
| `blocked` | Never installs |

```bash
genus plugin install genus-hostinfo --dry-run
genus plugin install genus-hostinfo
genus plugin list
genus plugin info genus-hostinfo
genus plugin disable genus-hostinfo
genus plugin remove genus-hostinfo
```

**The lockfile is your record of the decision.** One row per distribution:
name, version, manifest sha256, verdict, enabled, groups, when it was recorded.
The loader consults it before it imports anything — a disabled row never runs
its module body, and a manifest whose digest no longer matches is refused until
you `genus plugin sync`. A distribution with no row loads as it always did, so
the lockfile is opt-in until the first sync.

```bash
genus plugin sync
genus plugin doctor
```

A damaged lockfile never blocks boot. It is reported, damage is counted, and
the rows that are still readable still govern — but understand what that
means: while the file is damaged, **every plugin you switched off is loading
again**. The Helm's Settings › Plugins page says exactly that rather than
implying something still governs.

The full extension-point reference, what the scanner can and cannot see, and
the Helm's install flow are in [Plugins](../PLUGINS.md).

Next: [Secrets](05-secrets.md) — the credentials all of this runs on.
