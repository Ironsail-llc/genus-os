# Dev team report

The weekly dev team operations report is a workflow
(`docs/workflows/devops-report-pipeline.yaml`) of deterministic scripts with
one LLM step. Every number, name and role in the report comes from the
scripts. The analyst agent writes prose only, and a guard checks that prose
against the numbers.

## The week

All sources use one window: the previous ISO week, Monday 00:00 to the next
Monday 00:00 in America/New_York. Each script takes `--week-start YYYY-MM-DD`
(any day of the wanted week) to re-run or backfill a week.

## Where the files are

Each week's files go in `$DEVOPS_RUNS_DIR/<week-start>/`. The default is
`$ROBOTHOR_WORKSPACE/local/devops/runs`. `$DEVOPS_RUNS_DIR/latest` points at the
week the last aggregate built. Nothing is written to `/tmp`. The engine runs
with a private `/tmp`, so files there could not be inspected after a run.

| File | Written by | What it holds |
|---|---|---|
| `jira.json` | `scripts/devops_collect_jira.py` | Issues resolved in the week (assignee, type), in progress, open backlog size, stale tickets |
| `github.json` | `scripts/devops_collect_github.py` | Per repo: merged PRs, reviews, commits, stale PRs, `unavailable` metrics, `errors` |
| `usage.json` | `scripts/devops_claude_usage.py` | HR Claude usage rows, or `available: false` and a reason |
| `people.json` | `scripts/devops_aggregate_people.py` | The deterministic table: one row per active roster member, team totals, trends, data quality |
| `analyst.json` | the `devops-analyst` agent | Narrative only (contract below) |
| `report.html`, `report.json`, `meta.json` | `scripts/devops_render_report.py` | Email body, rendered context, subject and plain summary |

## Settings

| Variable | Meaning |
|---|---|
| `DEVOPS_TEAM_FILE` | The team roster YAML. Default `$ROBOTHOR_WORKSPACE/local/devops/team.yaml` |
| `DEVOPS_RUNS_DIR` | Root of the per-week run directories (see above) |
| `DEVOPS_GITHUB_REPOS` | Comma-separated `owner/repo` list |
| `DEVOPS_JIRA_PROJECTS` | Comma-separated JIRA project keys |
| `DEVOPS_JIRA_DONE_STATUSES` | Optional. Statuses that mean delivered while the issue stays unresolved (e.g. a final "Deployed" column). Entering one in the week counts as closed; such issues are not "in progress" or backlog |
| `DEVOPS_USAGE_REPORT_SENDERS` | Comma-separated sender addresses of the HR usage email |
| `DEVOPS_USAGE_LLM_MODEL` | Optional. A model used once when the usage PDF cannot be parsed deterministically |
| `DEVOPS_REPORT_RECIPIENTS` | Comma-separated email addresses. When set, used instead of the `devops_report_recipients` memory block |

## The team file

The report counts the people on this file and no one else.

```yaml
roles:                    # role key -> how this role is judged
  senior: {title: Senior Engineer, focus: "Architecture, reviews, unblocking others"}
  ai_reviewer: {title: AI Reviewer, focus: "Review coverage and turnaround"}
members:
  - id: alice             # stable key; the analyst's notes are keyed by it
    name: Alice Example
    role: senior          # must be a key under roles
    github: [alice-gh, alice-alt]
    jira: ["Alice Example"]   # JIRA display name(s)
    usage_name: Alice     # name in the HR usage report; omit if not tracked
    active: true          # false = former member: no row, activity listed separately
  - id: review-agent
    name: Review Agent
    role: ai_reviewer
    github: ["review-agent[bot]"]   # a roster handle counts even if it looks like a bot
bots_excluded: [dependabot[bot], github-actions[bot]]
```

The file is rejected if a handle belongs to two members, a role is unknown, or
a member id repeats. Other handles are handled like this:

- A handle that is not on the roster never gets a row. Its activity is listed
  under *outside roster*. A former member's handle (`active: false`) is listed
  there too.
- A handle in `bots_excluded`, or any non-roster handle ending in `[bot]`,
  appears only on the bots line.
- A commit that GitHub could not link to a login is listed by its git author
  email.

## What is counted

| Metric | Source and rule |
|---|---|
| Tickets shipped | Distinct ticket keys (of the configured JIRA projects) in the titles of the person's PRs merged in the week. Credits the developer even when the JIRA assignee has moved on to QA or a PM. |
| Tickets closed in JIRA | JIRA issues resolved (or moved into a `DEVOPS_JIRA_DONE_STATUSES` status) in the week, by current assignee, split by issue type. All pages are read. |
| PRs merged | PRs merged in the week into any base branch, by author |
| Lines +/− | Line changes from each merged PR's files. Lockfiles, generated, vendored and minified files are left out (`EXCLUDED_PATH_GLOBS` in `robothor/engine/reports/devops_github.py`). |
| Commits | Non-merge commits on the default branch in the week |
| Reviews | Reviews submitted in the week, split approved / changes requested / commented. Self-reviews are not counted. |
| Review turnaround | Median hours from PR creation to the reviewer's first review on that PR |
| Changes requested received | Changes-requested reviews in the week on the member's own PRs |
| Merged without review | Merged PRs with no review from anyone other than the author or a bot |
| Stale PRs | Open, non-draft PRs older than 14 days |

GitHub data comes from the REST listing endpoints and every page is read. The
Search API is not used, because its secondary rate limit caused the earlier
missing-repo failures. A request that still fails after retries is recorded in
`errors`. Each metric it affects is shown as **n/a**, with the reason, in the
report's data-quality block. A failed request is never counted as 0. A
collector with any error exits 1, but it still writes its file, so the rest of
the run can show an incomplete report.

## Claude usage (HR report)

`scripts/devops_claude_usage.py` gets the newest email from
`DEVOPS_USAGE_REPORT_SENDERS` sent in the last 10 days with "Claude Usage
Report" in the subject. It downloads the PDF attachment through the `gws` CLI
and reads it with `pdftotext -layout`.

The parser finds the column positions from the header row on every page,
because the column set changes from week to week (some weeks have a Fable
column). It then joins cells that wrap onto several lines. Each row records:

| Field | Values |
|---|---|
| `reading` | `post_reset`, `mid_cycle`, `unverified` or `completed`. Anything but `completed` is labelled *partial* in the report. |
| `limit_status` | `hit`, `projected`, `near` or `ok`. This is the weekly plan limit only. |
| `fable_limit_hit` | The separate Fable limit. It is never folded into `limit_status`. |

A percentage outside 0–100, a missing percentage, or a row count that does not
match the table means the parse failed. The script then makes one LLM
extraction call if `DEVOPS_USAGE_LLM_MODEL` is set. The same validation runs
on that output. If both fail, usage is reported as unavailable with the
reason.

Each week's PDF and parsed JSON are archived as
`$ROBOTHOR_WORKSPACE/local/devops/claude_usage/<report-date>.{pdf,json}`.

To backfill or parse a specific report, use `--message-id <gmail id>` or
`--pdf <path>`.

## The analyst contract

The analyst writes `analyst.json` and nothing else:

```json
{
  "summary": "2-4 sentences on the week",
  "per_person": {"<member id>": "one short note"},
  "bottlenecks": ["text", "..."],
  "si_vs_output": "how Claude usage relates to output"
}
```

Any other key is ignored. The renderer never takes a number or a role from
this file.

The guard (`check_narrative` in `robothor/engine/reports/devops_report.py`)
checks every number in a note that names a unit: PRs, commits, reviews,
tickets or %. The number must equal the member's value in the table. A note
fails the check if a number differs or if it gives a number for a metric that
is unavailable. A failed note is dropped and the report shows *narrative
withheld: numbers didn't match data*. A missing or broken `analyst.json` never
blocks the report.

## Delivery

- **Email.** `scripts/devops_send_emails.py` sends one message with every
  recipient in To:. Recipients come from `--to` first, then
  `DEVOPS_REPORT_RECIPIENTS`, then the memory block. The memory block may list
  email strings, `{"email": ...}` entries, or `{"name", "person_id"}` entries,
  which are looked up in the CRM. The script exits 1 if any recipient cannot be
  resolved. The recipients that were resolved still get the report.
- **Telegram.** The shipped workflow does not post to Telegram: the
  operator's chat is a conversation with the main agent, which reads
  `$DEVOPS_RUNS_DIR/latest/report.json` and mentions what matters in its next
  scheduled update. `scripts/devops_send_telegram.py` remains for instances
  that want a digest step; it posts the team totals and the data-quality flags.

## Step status

A workflow `exec` step whose command exits non-zero is recorded as **failed**.
With `on_failure: skip` it is recorded as *skipped*, and the exit code and the
end of stderr are kept. Such a step used to be recorded as *completed*.

## Commands

```bash
venv/bin/python scripts/devops_collect_jira.py      [--week-start D] [--runs-dir P]
venv/bin/python scripts/devops_collect_github.py    [--week-start D] [--runs-dir P]
venv/bin/python scripts/devops_claude_usage.py      [--week-start D] [--runs-dir P] [--message-id ID | --pdf PATH] [--archive-dir P]
venv/bin/python scripts/devops_aggregate_people.py  [--week-start D] [--runs-dir P] [--team-file P]
venv/bin/python scripts/devops_render_report.py     [--week-start D] [--runs-dir P]
venv/bin/python scripts/devops_send_emails.py       [--week-start D] [--runs-dir P] [--to a@x,b@y]
venv/bin/python scripts/devops_send_telegram.py     [--week-start D] [--runs-dir P]
```
