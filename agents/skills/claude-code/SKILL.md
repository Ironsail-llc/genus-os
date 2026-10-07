---
name: claude-code
description: Delegate a coding task to Claude Code and see it through to a verified commit — spec, acceptance, wait, follow up, report evidence
tags: [coding, delegation, claude-code]
tools_required: [claude_code_start, claude_code_wait, claude_code_status, claude_code_followup, claude_code_cancel]
parameters:
  - name: goal
    type: string
    description: What the operator wants changed, in their words
    required: true
  - name: repo_path
    type: string
    description: Absolute path of the git repository
    required: true
---

# Delegating a coding task to Claude Code

You are the orchestrator. Claude Code is the one who writes the code: it runs
in its own git worktree, on its own branch, with a shell. Your job is to give
it a spec it cannot misread, define what "done" means in a form a machine can
check, and report what the check found. You never write the code yourself and
you never report a claim — only evidence.

## 1. Write the spec

Turn `{goal}` into a task Claude Code can finish without asking anything:

- **What** to change, in one sentence.
- **Where**: the files, modules or functions involved, if you know them. If you
  do not, say what to look for.
- **Constraints**: what must not change (public APIs, file formats, other tests).
- **Done when**: the observable result, in the same words as the acceptance check.

Keep it short. A spec longer than the change is a sign you are designing the
code instead of describing the outcome.

## 2. Define acceptance — a command, not an opinion

`acceptance.verify_command` must exit 0 **only** when the task is done. The
engine runs it itself in the worktree after every round; Claude Code saying
"done" counts for nothing.

- Prefer the narrowest real check: `pytest -q tests/test_parser.py`, not the
  whole suite (slow) and not `true` (meaningless).
- For a new behaviour, the check is a test that exercises it. If none exists,
  say in the spec that Claude Code must add one and name the test file, then
  use that file in the verify command.
- It runs without a shell. For a pipeline write `bash -c '...'` explicitly.
- Leave `require_commit` true for code changes: the job is not done until the
  work is committed on the job branch with a clean tree.
- `review` and `readonly` jobs take no `verify_command`: they are judged by
  their answer (ask for one with `json_schema`).
- The repository must sit under `ROBOTHOR_CODING_REPO_ROOTS`; the live Genus
  workspace itself is refused. The job's shell has no network unless the
  operator allowed domains, so a task that needs to install packages must say
  so to the operator rather than retry.

## 3. Start, then wait

```
claude_code_start(task=<spec>, repo_path="{repo_path}",
                  acceptance={"verify_command": "<cmd>", "require_commit": true})
→ job_id
claude_code_wait(job_id, timeout_s=600)
```

A job still `running` after the wait is normal — wait again. Use
`claude_code_status` to see what it is doing without blocking (`recent` lists
its last actions). Do not start a second job for the same change.

The engine already retries for you: a failed verify resumes the same Claude
Code session with the failing output, up to `max_rounds` (default 3), within
`max_budget_usd` (default 5).

## 4. When it fails, follow up with specifics

A `failed` job tells you why in `error` and `verify.output_tail`. Read them.
Then either:

- **Follow up** in the same session with something Claude Code did not already
  know: the root cause you can see, a file it missed, a constraint it broke.
  `claude_code_followup(job_id, "test_x fails because parse() returns None on
  empty input; handle the empty case and keep the existing signature")`.
  "Please try again" adds nothing — the engine already said that.
- **Change the acceptance** only if the check itself was wrong — and say so in
  your report.
- **Cancel** (`claude_code_cancel`) if the task was misconceived, and tell the
  operator what you learned.

## 5. Report evidence, never a claim

A `done` job carries evidence. Report exactly these, from the tool result:

- the **job id**,
- the **branch** and **commit sha** (`commit_sha`),
- the **verify command** and its result (`verify.exit_code`, and the pytest
  summary if there is one),
- the cost (`cost_usd`) and rounds used.

If a goal is open, record the job's `evidence` items on it (they are already in
the `test_run` / `commit` shape goals accept). The work is on a branch, not
merged and not pushed: say so, and say what the next step is (review, PR).

If the job is not `done`, say that plainly — "the job failed after 3 rounds:
<reason>" — and never describe the change as made.
