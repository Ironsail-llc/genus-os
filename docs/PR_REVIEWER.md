# PR reviewer

The pr-reviewer suite reviews pull requests with Claude Code and posts the
result live on GitHub: blocking findings as inline comments, everything else in
the review body, a verdict the platform recomputes from the findings, and a
short announcement in the Google Chat thread where the link was posted. It is
off until an instance configures it.

## How it works

```
cron */2  pr-review-intake workflow ── pr_review_intake ──┐  (no model, one run
          GitHub: open PRs in configured repos            │   per tenant: lock)
                  + PRs in them requesting the bot        ├─► one CRM task per PR head
          Chat:   PR links (claim 👀), "ptal" replies     │   tag pr-review → pr-reviewer
                                                          ┘
cron */2  pr-review-run workflow ── count_only: queued? ──► pr-reviewer agent (cheap model)
            pr_review_prepare  → clone fetched to the head, review prompt, rules from
                                 the BASE branch; starts the read-only review job
                                 (mode=review) itself and binds its job_id
            claude_code_wait
            pr_review_finalize → only the bound job, once: validate → redact →
                                 recompute verdict → post → reply on previous
                                 threads → resolve own threads on APPROVE →
                                 announce in Chat → record state
            resolve_task       ← evidence: the review URL
```

**Intake is deterministic.** `pr_review_intake` never calls a model. A
top-level Chat message linking a pull request in an allowed repository claims
it with a reaction and queues an initial review. A thread reply **from the
person who posted the link** is classified by keyword: "re-review", "ptal",
"fixed", "pushed", "can you re-review?" and similar queue a re-review; an
acknowledgement ("thanks", 👍) or a reviewer's status line ("#12: Approved",
"#12: Comments/change request", "changes requested" — including another
review bot's) does nothing; anything negated or unclear ("not fixed yet",
"will push later", "is this fixed?") is handed to the agent as a task to
decide. In a thread that posted several pull requests, a reply that names some
of them ("12 is ready for re-review", "#12", a link) re-reviews only those. Every Chat
message is handled once, and one that fails is recorded with its error in
`pr_review_messages` and skipped — never retried. The Chat cursor survives
restarts.

**One task per head.** A new head SHA, or a re-review request, files at most
one CRM task, deduplicated by repo + number + SHA, up to
`ROBOTHOR_PR_REVIEW_MAX_CONCURRENT` open at once. A request that arrives
while a review runs waits for it. A re-review covers only what changed since
the last reviewed SHA, unless history was rewritten (force-push, rebase), in
which case it is a full review again. Lockfile-only changes (matched by file
name or suffix: `yarn.lock`, `*.snap`, `*.min.js`) and dependency-bot pull
requests are skipped; a label skips a review only if it is listed in
`ROBOTHOR_PR_REVIEW_SKIP_LABELS` (empty by default). Changes under 50 lines get
a light review. A pull request posted in Chat that is closed, merged, a draft
or skipped gets a thread reply saying so.

**Exactly one review per head.** `pr_review_prepare` starts the review job
itself and records its `job_id` and head on the row (`status=reviewing`);
`pr_review_finalize` accepts only that job and claims it with a
compare-and-set (`reviewing → posting`) before posting anything. Each posting
step is recorded, so a finalize retried after a timeout resumes where it
stopped, and a finalize for an already-posted job returns that review
(`already_posted: true`) instead of posting another. `github_create_review`
is itself idempotent per head, the second layer. The intake holds a
per-tenant advisory lock; the run workflow only counts the queue, so only the
intake ever files tasks. Row saves write only the changed columns, so the
intake and finalize never revert each other.

**The verdict is code, not the model's word.** `pr_review_finalize` reads the
job's structured output from the coding job itself — the agent never relays
it — and recomputes the verdict (`robothor/pr_review/policy.py`):

- any `blocker` or `major` finding, or a previous one the model did not
  explicitly report `resolved` — silence is not a fix — posts
  `REQUEST_CHANGES` (or `COMMENT` with
  `ROBOTHOR_PR_REVIEW_BLOCKING_EVENT=COMMENT`) — never `APPROVE`. Previous
  findings are tracked by the inline comment id GitHub gave each one, mapped
  by posting order, so two findings on one line never share an id;
- `APPROVE` only when the model approved and nothing blocking remains;
- with `ROBOTHOR_PR_REVIEW_REQUIRE_TICKET=true`, a pull request with no ticket
  key in its title, branch, description or commit messages gets a blocking
  `[no-ticket]` finding.

`github_create_review` itself also refuses an APPROVE beside a blocking
finding, for every caller. The summary, every finding, previous-finding notes
and the Chat announcement pass through the secret redactor
(`robothor/secrets/redaction.py`) before they are posted or stored.

**What the review job may do.** It runs in its own read-only worktree at the
pull request's head with Read, Grep, Glob and read-only `git`, and no network
(no `gh`, no web). It cannot edit, push or comment. Claude Code runs it with
`--effort` `ROBOTHOR_PR_REVIEW_EFFORT` (default `high`), up to
`ROBOTHOR_PR_REVIEW_MAX_TURNS` turns (80) per round, 1800 s per round
(`ROBOTHOR_PR_REVIEW_ROUND_TIMEOUT`) and a $25 notional cap
(`ROBOTHOR_PR_REVIEW_BUDGET_USD`). The review guidelines are the instance's
own file when `ROBOTHOR_PR_REVIEW_GUIDELINES_PATH` names a readable one, else
the `pr-review` skill (twelve lenses, severities, verify-or-drop, a
completeness pass, re-review rules); either way the prompt adds the operating
notes (real tools, output fields, the verdict rule) and the repository's own
`.github/review-guidelines.md`, `CLAUDE.md` or `AGENTS.md` read from the
**base** branch (`origin/<base>`), when present — a pull request cannot
rewrite the rules it is reviewed against.

**Who may call what.** The pr-reviewer agent opts into
`claude_code_status/_wait/_cancel` and `pr_review_prepare/_finalize` only: it
cannot start an arbitrary Claude Code job or post, reply on or resolve a
review itself. Migration 146 makes the rest a permission: `user` and `member`
callers are denied `github_create_review`, `github_reply_review_comment`,
`github_resolve_threads`, `claude_code_start/_followup/_cancel` and the three
`pr_review_*` tools; `service` — the role every unattended run dispatches as,
including both workflows and the pr-reviewer agent step — is denied only the
three `github_*` review-write tools, because finalize calls their handlers
directly. `owner` and `admin` keep everything.

**The linked ticket.** prepare finds a ticket key in the title, branch,
description or commit messages (trailers included), using the repository's
prefix from `owner/repo:PREFIX` entries in `ROBOTHOR_PR_REVIEW_REPOS` or
`_TICKET_PREFIXES` (bare `PREFIX` entries apply to every other repository).
It fetches the issue through the Jira integration (`jira_get_issue` with
`include_text`, no model; `JIRA_BASE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`)
and puts its summary, status, description and acceptance criteria — redacted,
at most 8,000 characters — in the prompt, telling the model to check the pull
request against each criterion. When Jira is not configured or the fetch
fails, the prompt says so and forbids inventing criteria.

**Asking for a review.** Main opts into `pr_review_intake`: when the operator
asks it to review a pull request it calls `pr_review_intake(pr="<url or
owner/repo#N>")`, which queues that one pull request (configured repositories
only) for the pr-reviewer and returns `requested.status`; calling it again
reports `requested.review_url` once the review is posted. Such a review has no
Chat thread; its finalize result carries a Telegram digest line, which reaches
Telegram when the pr-reviewer is installed with `delivery_mode=announce`. Main
runs as `owner` on Telegram, which migration 146 leaves allowed.

**Timeouts.** `claude_code_wait` enforces its own wait (up to 1800 s), so the
registry gives it 1830 s; `pr_review_prepare` and `pr_review_finalize` get the
600 s long-running floor. A git clone or fetch that is cancelled is killed,
never left running.

## Enabling it on an instance

1. **Claude Code and GitHub.** Run `genus claude-code login` once (see
   [Tools](TOOLS.md#claude-code-claude_code_)). Store a `GITHUB_TOKEN` in the
   vault for the account the reviews should come from; it needs read access
   to the repositories and permission to review pull requests.
2. **Install the agent.** `genus agent install pr-reviewer` (catalog
   department `engineering`). For a Telegram digest of each review, install it
   with `delivery_mode=announce` and set `ROBOTHOR_PR_REVIEW_TELEGRAM_DIGEST=true`.
3. **Install the workflows.** Copy `templates/workflows/pr-review-intake.yaml`
   and `templates/workflows/pr-review-run.yaml` into the instance's
   `docs/workflows/`, and exclude them from git locally (append both paths to
   `.git/info/exclude`) — `docs/workflows/` is tracked, and which repositories
   an instance reviews is instance data.
4. **Configure** (environment or `config.yaml`, group `pr_review`):

   | Setting | What to set |
   |---|---|
   | `ROBOTHOR_PR_REVIEW_REPOS` | `owner/repo,owner/other` — the repositories it may review; `owner/repo:PREFIX` also sets that repository's ticket prefix |
   | `ROBOTHOR_PR_REVIEW_WATCH_REPOS` | `false` (default) reviews only pull requests posted in Chat or requesting the bot; `true` reviews every open pull request in the repos |
   | `ROBOTHOR_PR_REVIEW_CHAT_SPACE` | `spaces/…` to watch and announce in; empty for GitHub only |
   | `ROBOTHOR_PR_REVIEW_CHAT_SELF_USERS` | the `users/…` the gws CLI posts as, so its own messages are never requests |
   | `ROBOTHOR_PR_REVIEW_BOT_LOGIN` | GitHub login whose requested reviews it picks up |
   | `ROBOTHOR_PR_REVIEW_REQUIRE_TICKET` / `_TICKET_PREFIXES` | the optional ticket rule; prefixes as `owner/repo:PREFIX` or bare `PREFIX` |
   | `ROBOTHOR_PR_REVIEW_GUIDELINES_PATH` | the team's own review guidelines (see [parity](#parity-with-an-existing-review-bot)) |
   | `ROBOTHOR_PR_REVIEW_MODEL` | the Claude Code model for reviews (e.g. `opus`); empty uses `ROBOTHOR_CLAUDE_CODE_MODEL` |

   [Settings](reference/configuration.md#pr_review) lists every
   `ROBOTHOR_PR_REVIEW_*` value. If `ROBOTHOR_CODING_REPO_ROOTS` is set, it
   must include the clone root (`ROBOTHOR_PR_REVIEW_CLONE_ROOT`, default
   `<workspace>/.genus/pr-review/repos`).
5. **Apply migrations 145** (`pr_reviews`, `pr_review_messages`,
   `pr_review_cursors`), **146** (tool permissions) **and 147** (per-job
   Claude Code limits on `coding_jobs`) and restart the engine.

**Cutover from another review bot.** Never run the suite on the same
repositories as another automated reviewer: both would claim the same links
and post two reviews per head. Stop the other bot first, or start with a
disjoint `ROBOTHOR_PR_REVIEW_REPOS` and move repositories over one at a time.

## Parity with an existing review bot

The suite is built to replace a team's own review bot (Claude Code over the
GitHub and Jira APIs, triggered from a Chat space) without the team noticing a
change in what it reads, and to beat it where it can. To reproduce one:

| Old bot setting | Genus setting |
|---|---|
| `ALLOWED_REPOS=owner/repo:PREFIX,…` | `ROBOTHOR_PR_REVIEW_REPOS` — the same value |
| `REVIEW_GUIDELINES_PATH` | copy the file to `brain/pr-review-guidelines.md` (instance data, gitignored with `brain/*.md`) and set `ROBOTHOR_PR_REVIEW_GUIDELINES_PATH=<workspace>/brain/pr-review-guidelines.md` |
| `REVIEW_MODEL` / `REVIEW_EFFORT` / `REVIEW_MAX_TURNS` / `REVIEW_TIMEOUT_MS` | `ROBOTHOR_PR_REVIEW_MODEL=opus`, `_EFFORT=high`, `_MAX_TURNS=80`, `_ROUND_TIMEOUT=1800` (the last three are the defaults) |
| `CHAT_SPACE` / `CHAT_SELF_USER_ID` | `ROBOTHOR_PR_REVIEW_CHAT_SPACE` / `_CHAT_SELF_USERS` |
| `CHAT_CLAIM_REACTION` / `CHAT_APPROVED_REACTION` | `ROBOTHOR_PR_REVIEW_CLAIM_REACTION` (👀) / `_APPROVED_REACTION` (👍) |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | `JIRA_BASE_URL`, `JIRA_USER_EMAIL`, and `JIRA_API_TOKEN` in the vault |
| `REVIEW_MODE=auto` | always: reviews are posted live, exactly once |

**Kept as the team knows it.** The guidelines file verbatim, framed the same
way ("the guidelines win on conflict", the operating notes, the re-review
section with the previous summary, previous issues and the incremental compare
range); the ticket and its acceptance criteria as the baseline; 👀 on the
claimed message and 👍 replacing it on approval; thread replies prefixed with
the linked PR number: `#N: Approved`, `#N: Comments/change request`,
`#N: No changes?` (a re-review request with no new commits), "A review is
already running for this PR…" (a request while one runs), "This PR is merged;
skipping the review.", and "⚠️ Automated review failed after N attempts: …
Reply "re-review" to try again.".

**Deliberately better.**

- **Blocking findings never get through.** The verdict is recomputed in code:
  a blocker or major finding, or a previous one not reported `resolved`, is
  `REQUEST_CHANGES`, whatever the model proposed. The old bot posted the
  model's verdict.
- **Previous findings are followed up, not just listed.** Each one is tracked
  by its inline comment id; the re-review replies on its thread (resolved,
  partially resolved, still open) and resolves our threads only on approval.
- **The prompt promises only what the job can do.** A review job has no
  network, so the operating notes say so and map guideline steps it cannot run
  (`gh pr diff`, sub-agents, Jira tools) to what it can (`git diff
  origin/<base>...HEAD`, separate passes, the ticket fetched for it), instead
  of leaving the model to fail at them.
- **The ticket is fetched for the model**, redacted and size-capped, and a
  missing or unreadable ticket is said plainly — the model is told not to
  invent criteria.
- **The Chat reply adds the blocking count**: `#N: Comments/change request —
  2 blocking`; the familiar prefix stays. A re-review requested while a review
  runs gets "No changes?" only if it was not already told a review is running.
- **Rules come from the base branch**, so a pull request cannot rewrite the
  `CLAUDE.md` it is reviewed against; output is redacted before posting; the
  job is sandboxed read-only.
- **The assistant can be asked** to review one pull request
  (`pr_review_intake(pr=...)`), and reports the posted review.

## Operating it

- `pr_review_intake` returns counts (`tasks_created`, `deferred`, `retries`,
  `chat_errors`, `open_tasks`, `queued_tasks`, `errors`) in each workflow run,
  or `{"skipped": "locked"}` when another intake run holds the tenant's lock.
- `pr_reviews` holds one row per pull request: `status` is `pending`,
  `queued`, `reviewing`, `posting`, `approved`, `changes_requested`,
  `commented`, `failed`, `skipped` or `closed`; `job_id` is the review job
  prepare bound; `error` says why a review failed.
- A failed review (job failure, head moved, posting error, or a review that
  never finalized within `ROBOTHOR_PR_REVIEW_STALE_AFTER_MINUTES`) is retried
  at once on a new head, and on the same head after
  `ROBOTHOR_PR_REVIEW_RETRY_COOLDOWN_MINUTES` (default 60), at most 3 attempts
  per head; the author can always reply "re-review" in the Chat thread. A
  failure on a pull request with no Chat thread is reported in the Telegram
  digest when `ROBOTHOR_PR_REVIEW_TELEGRAM_DIGEST` is on.
