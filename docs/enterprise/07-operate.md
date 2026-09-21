# 7. Operate it

Goal: know which page answers which question before you need one at three in
the morning.

## The doctor first, always

```bash
genus doctor
genus doctor --category database
genus doctor --only db.migrations
genus doctor --json
```

It is the single answer to "is this instance actually working?", and it is the
first thing to run for any report that starts with "something is wrong". `✗` is
a failure, `·` is a check that could not run, and **a skip is never a pass**.
Exit 1 means a `required` check failed; exit 2 means the doctor itself could
not run, which is a different problem.

The Helm's Health view serves the same report from the Bridge, offline and
memoised for thirty seconds behind a single-flight lock. That memoisation is
not laziness: online, every refresh would make a paid completion, a channel
call and a fork of a host script, so two operator tabs at thirty-second
intervals would be thousands of provider calls a day caused by a dashboard. The
three checks that cost money, leave the box or fork report themselves `skip`
with the reason. **Run the CLI when you need an answer taken just now.**

## The Observe pages

Four screens over the Bridge, behind the operator gate. The browser's
navigation is a convenience; the Bridge checks the caller's role on every
request regardless of what the page shows.

| Page | Answers | Who sees it |
|---|---|---|
| **Runs** and **Fleet** | What ran, when, what it delivered, which agents are scheduled and which are off | owner, admin |
| **Memory** | What the instance believes, and what a forget would actually do | owner, admin |
| **Audit** | Every audited event, plus the guardrail change log, with a CSV export | owner, admin, **auditor** |
| **Logs** | A unit's journal, redacted | owner, admin |

**Memory.** Search, an entity filter, and an active/inactive/all switch.
Browsing pages with a cursor; searching does not, because a relevance ranking
has no place to resume from.

**Forget shows its consequences first**, and the part worth reading is what it
does *not* do. It bounds one row — inactive, valid-to now — and nothing else.
Nothing is deleted, no supersession chain is followed, and **a memory block
that quotes the text still quotes it**, so an agent carrying that block keeps
repeating the fact until the block is rewritten. The preview names how many
episodes cite the fact and which always-in-context blocks quote it, and the
reason you type goes into the audit record. A fact under twelve characters is
too short to check against the blocks, and the page says so rather than
claiming there are none.

**Audit** has the audit log and the guardrail change log, and **Export CSV**
carries the same filters the table is showing, up to five thousand rows. Two
things to know before you hand one to a regulator: the export is
**appliance-wide**, because the audit table has no tenant column, so the tenant
in the filename names who exported it and not what is inside; and every cell is
sanitised, redacted and prefixed where a spreadsheet would evaluate it, so an
audit detail cannot become a formula or carry a credential off the box.

**Logs need journald**, and a container does not have it. Both routes answer
"not available on this deployment" with a 200 and a reason, and the page says
that in the Bridge's own words rather than showing an error — nothing is
broken; use the container runtime's log stream instead. Where journald is
present, the unit list is derived from the units the installer wrote, so a new
unit appears without a code change. Auto-refresh is off by default and beats
every ten seconds when on, because each read is one process on the box. The
filter applies to the **redacted** message — the pane cannot be used to confirm
a value it will not show you.

## Flags, and the observe-to-enforce ladder

**Settings › Flags** is every governed flag, its rungs, and the verdict: what
the control has done, and when it last did it. A change is live within seconds,
needs no restart, and records the reason you type beside it — that reason is
the row you will want in six weeks.

Every guardrail worth having goes up the same ladder:

| Rung | What it does | Leave it here until |
|---|---|---|
| `off` | Nothing | — |
| `observe` | Evaluates and records; changes no outcome | The evidence table has rows, and you have read what it would have done |
| `alert` | Records **and** pages, still without blocking | You believe the page is one an operator should act on |
| `enforce` | Acts | — |

**The rungs are per flag, and the Flags page is where you read them, not this
table.** Four rungs is the default shape; a few controls have three (no
`alert`, because they block nothing there would be anything to page about) and
a couple have two (`ROBOTHOR_DNC_MODE` is a compliance opt-out, so it has no
`off` at all). Setting a rung a flag does not honour is worse than being
refused — you would see it stored and get different behaviour — which is why
the platform keeps one list, `valid_values_for` in `robothor/flags/store.py`,
and the page renders that rather than a table someone typed.

Two rules learned the expensive way, and they are why this page exists:

1. **A quiet evidence table is not a passing control.** It is at least as
   likely to be a control that is not reaching its target at all. Before you
   promote anything, fire a real violation and watch it get caught. A green
   test suite has certified an inert control more than once here.
2. **Never verify a control from a log line it prints itself.** The line proves
   the code ran, not that it did anything. Look for the side effect.

Some controls ship a positive control of their own — a doctor check that
exercises the real path and fails against the old behaviour. Use it. That is
what it is for.

## Alerting

Alerts reach an operator over a channel, so they inherit everything on
[Channels](03-channels.md) — including the fact that a channel reporting
"configured" is not a channel that has delivered anything. Prove the path with
`genus channel verify` on the channel your pages go to, and do it again after
any credential change.

Two failure modes to design against, because both have happened:

- **On-box alerting is silent when the box is the problem.** Something outside
  the deployment has to notice that the deployment stopped speaking.
- **A delivery that returned an error is not a delivery.** Check the recorded
  `delivery_status`, not that the alerting code ran.

`genus doctor` in a scheduled job, with its exit code treated as the signal, is
the cheapest useful external check you can build.

## The benchmark rotation as a regression gate

If agent quality matters to you, measure it on a schedule rather than
noticing a regression through a complaint. Two rules:

- **Run it in the sandbox.** The harness runs real agents with real tools; run
  it against production data and it will take real actions. The
  [benchmark sandbox](../runbooks/BENCHMARK_SANDBOX.md) is the isolation that
  makes a benchmark safe to run at all, and it is not optional.
- **Compare like with like.** A harness that gives one system a longer budget,
  a different tool surface or a different prompt is measuring the harness.
  `docs/runbooks/BENCHMARK_HARNESS_FAIRNESS.md` in the repository is the
  fairness checklist, and the single most common cause of a surprising result
  is that the comparison was not fair.

Treat a single run as noise. Run-to-run variance is wide enough that one run
settles nothing, so it is the trend across runs that is the signal.

## Traces

Set the OTLP endpoint and the platform emits spans for agent runs, tool calls
and model calls into whatever you already run.
[Observability](../OBSERVABILITY.md) has what is emitted, the failure-mode
detectors, and — the section to read first — the difference between a run that
**ran**, a run that **delivered**, and a run that **completed** its objective.
Those are three different questions and a dashboard that conflates them will
tell you everything is fine.

## Where to look when something is wrong

For governed prospect research and outreach, open **Sales** and follow
[Sales intelligence](../SALES_INTELLIGENCE.md). Its deployment gates distinguish
automated contract tests from a verified live sales pilot.

In Sales, **Review imported practices** confirms customer identity matches;
**Inspect provider reads** shows failed imports and message reads, with a reason
required before retrying a repaired read. Each outbound message still has its
own approval.

Use **Repair customer match** for incorrect practice ownership. It requires a
separate review and holds both affected conversations while their corrected
context is reviewed.

| Symptom | Start here |
|---|---|
| Anything at all, first response | `genus doctor` |
| Nobody can sign in | `genus doctor --category secrets`, then [Identity](02-identity.md) — the classic cause is a sign-in secret present in one place and absent where the process reads |
| An agent is not running | Its `schedule.enabled`, then the Automations view, then Observe › Runs |
| An agent ran and nothing arrived | The run's `delivery_status`, then `genus channel verify <channel>` |
| A plugin stopped loading | `genus plugin doctor`, then the lockfile — a damaged one means every disable is off |
| The schema looks wrong | `genus migrate --status`. Read it; do not force anything |
| An agent lost a credential | `genus doctor --category secrets`, then [Secrets](05-secrets.md) |
| Answers got worse | The flags page: what changed, and when. Then the benchmark rotation |

The repository's own [Reading Guide](../READING_GUIDE.md) is the wider map, for
when the answer is in code rather than in a page.

You have now done the whole guide. Back to [the overview](00-overview.md).

Sales also accepts bounded research requests and provides **Review integration setup**
for credential-name readiness and reviewed mailbox/source configuration. Measured
reports keep incomplete or immature customer retention windows explicitly unknown.
