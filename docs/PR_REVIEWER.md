# PR reviewer

The pr-reviewer suite reviews pull requests with Claude Code and posts the
result live on GitHub: blocking findings as inline comments, everything else in
the review body, a verdict the platform recomputes from the findings, and a
short announcement in the Google Chat thread where the link was posted. It is
off until an instance configures it.

## How it works

```
cron */2  pr-review-intake workflow ── pr_review_intake ──┐  (no model)
          GitHub: open PRs in configured repos            │
                  + PRs requesting the bot's review       ├─► one CRM task per PR head
          Chat:   PR links (claim 👀), "ptal" replies     │   tag pr-review → pr-reviewer
                                                          ┘
cron */2  pr-review-run workflow ── queued? ──► pr-reviewer agent (cheap model)
            pr_review_prepare  → clone fetched to the head, review prompt, schema
            claude_code_start  → read-only review job (mode=review)
            claude_code_wait
            pr_review_finalize → validate → recompute verdict → post → reply on
                                 previous threads → resolve own threads on
                                 APPROVE → announce in Chat → record state
            resolve_task       ← evidence: the review URL
```

**Intake is deterministic.** `pr_review_intake` never calls a model. A
top-level Chat message linking a pull request in an allowed repository claims
it with a reaction and queues an initial review. A thread reply **from the
person who posted the link** is classified by keyword: "re-review", "ptal",
"fixed", "pushed" and similar queue a re-review; an acknowledgement ("thanks",
👍) does nothing; anything negated or unclear ("not fixed yet", "will push
later", a question) is handed to the agent as a task to decide. Every Chat
message is handled once, and one that fails is recorded with its error in
`pr_review_messages` and skipped — never retried. The Chat cursor survives
restarts.

**One task per head.** A new head SHA, or a re-review request, files at most
one CRM task, deduplicated by repo + number + SHA, up to
`ROBOTHOR_PR_REVIEW_MAX_CONCURRENT` open at once. A request that arrives
while a review runs waits for it. A re-review covers only what changed since
the last reviewed SHA, unless history was rewritten (force-push, rebase), in
which case it is a full review again. Lockfile-only changes and dependency-bot
pull requests are skipped; changes under 50 lines get a light review.

**The verdict is code, not the model's word.** `pr_review_finalize` reads the
job's structured output from the coding job itself — the agent never relays
it — and recomputes the verdict (`robothor/pr_review/policy.py`):

- any `blocker` or `major` finding, or a previous one reported unresolved or
  partially resolved, posts `REQUEST_CHANGES` (or `COMMENT` with
  `ROBOTHOR_PR_REVIEW_BLOCKING_EVENT=COMMENT`) — never `APPROVE`;
- `APPROVE` only when the model approved and nothing blocking remains;
- with `ROBOTHOR_PR_REVIEW_REQUIRE_TICKET=true`, a pull request with no ticket
  key in its title, branch, description or commit messages gets a blocking
  `[no-ticket]` finding.

`github_create_review` itself also refuses an APPROVE beside a blocking
finding, for every caller.

**What the review job may do.** It runs in its own read-only worktree at the
pull request's head with Read, Grep, Glob, read-only `git` and
`gh pr view/diff/checks`. It cannot edit, push, comment or call `gh api`. The
review content comes from the `pr-review` skill (twelve lenses, severities,
verify-or-drop, a completeness pass, re-review rules) plus the repository's own
`.github/review-guidelines.md`, `CLAUDE.md` or `AGENTS.md` at the head, when
present.

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
   | `ROBOTHOR_PR_REVIEW_REPOS` | `owner/repo,owner/other` — the repositories it may review |
   | `ROBOTHOR_PR_REVIEW_WATCH_REPOS` | `false` (default) reviews only pull requests posted in Chat or requesting the bot; `true` reviews every open pull request in the repos |
   | `ROBOTHOR_PR_REVIEW_CHAT_SPACE` | `spaces/…` to watch and announce in; empty for GitHub only |
   | `ROBOTHOR_PR_REVIEW_CHAT_SELF_USERS` | the `users/…` the gws CLI posts as, so its own messages are never requests |
   | `ROBOTHOR_PR_REVIEW_BOT_LOGIN` | GitHub login whose requested reviews it picks up |
   | `ROBOTHOR_PR_REVIEW_REQUIRE_TICKET` / `_TICKET_PREFIXES` | the optional ticket rule |

   [Settings](reference/configuration.md#pr_review) lists every
   `ROBOTHOR_PR_REVIEW_*` value. If `ROBOTHOR_CODING_REPO_ROOTS` is set, it
   must include the clone root (`ROBOTHOR_PR_REVIEW_CLONE_ROOT`, default
   `<workspace>/.genus/pr-review/repos`).
5. **Apply migration 145** (`pr_reviews`, `pr_review_messages`,
   `pr_review_cursors`) and restart the engine.

**Cutover from another review bot.** Never run the suite on the same
repositories as another automated reviewer: both would claim the same links
and post two reviews per head. Stop the other bot first, or start with a
disjoint `ROBOTHOR_PR_REVIEW_REPOS` and move repositories over one at a time.

## Operating it

- `pr_review_intake` returns counts (`tasks_created`, `deferred`,
  `chat_errors`, `open_tasks`, `queued_tasks`, `errors`) in each workflow run.
- `pr_reviews` holds one row per pull request: `status` is `pending`,
  `queued`, `reviewing`, `approved`, `changes_requested`, `commented`,
  `failed`, `skipped` or `closed`; `error` says why a review failed.
- A failed review is retried on the next push, or when the author replies
  "re-review" in the Chat thread. A review that never finalizes is marked
  failed after `ROBOTHOR_PR_REVIEW_STALE_AFTER_MINUTES` so it stops holding a
  concurrency slot.
