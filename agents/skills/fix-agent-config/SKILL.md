---
name: fix-agent-config
description: Diagnose and fix sub-agent configuration issues in YAML + instruction
  files that cause timeouts, tool failures, or degraded performance
tags:
- agent-config
- debugging
- yaml
- sub-agents
- troubleshooting
parameters:
- name: agent_id
  type: string
  description: Agent ID to fix (e.g. crm-dedup, email-responder)
  required: true
tools_required:
- read_file
- write_file
- exec
- list_agent_runs
- get_agent_run
- spawn_agent
---

# Fix Sub-Agent Configuration

**When to use:** A sub-agent is timing out, failing silently, producing wrong output, or not using the tools you expect. The root cause is usually in YAML config, instruction files, or a mismatch between the two.

## Diagnosis Sequence

### Step 1: Confirm the agent is actually failing
- `list_agent_runs(agent_id=<agent>, status="timeout")` — check for timeout pattern
- `list_agent_runs(agent_id=<agent>, status="failed")` — check for crash pattern
- `get_agent_run(run_id=<id>)` — read the audit trail. Look for:
  - Repeated tool call failures (wrong tool name → 404)
  - Agent looping on exec calls that hang
  - Agent never reaching its actual work (stuck in setup)

### Step 2: Check YAML config
Read `docs/agents/<agent_id>.yaml`. Verify:

1. **`tools_allowed` lists only tools that exist.** Tool names must match the engine's tool registry exactly. Common mismatches:
   - `merge_contacts` vs `merge_people` (CRM tool was renamed)
   - `create_contact` vs `create_person`
   - Tool names with underscores vs hyphens
   - Custom tools that were removed or renamed during upgrades

2. **`tools_allowed` does NOT include `exec`** unless the agent specifically needs shell access. `exec` in tools_allowed is the #1 cause of sub-agent timeouts because:
   - Agents spawn subprocesses that inherit the timeout clock
   - Agents use exec to call CLI tools that hang waiting for input
   - Agents write temp files via exec instead of using native tools
   - **Fix:** Remove `exec` from `tools_allowed`, add to `tools_denied`

3. **`tools_denied` blocks dangerous tools.** At minimum, sub-agents should deny: `exec`, `write_file` (unless they need it), `desktop_*` tools.

4. **Timeout and budget settings are reasonable.** Check `timeout_seconds` and `max_iterations` — too low causes premature cutoff, too high causes cost explosions.

### Step 3: Check instruction file
Read `brain/agents/<AGENT_ID>.md` (or `docs/agents/<agent>.md`). Look for:

1. **Inline code blocks that should be native tools.** If the instructions contain Python/shell code blocks the agent is expected to run via `exec`, replace them with native tool usage:
   - `exec python3 -c "..."` for data processing → use in-reasoning analysis instead
   - `exec curl ...` for API calls → use native tools (web_fetch, gws_gmail_*, etc.)
   - `exec psql ...` for database queries → use CRM tools (list_people, list_tasks, etc.)
   - `exec gog gmail send --reply-all` → BANNED. Use `gws_gmail_reply`.

2. **Steps that assume exec is available.** Rewrite each step to use the agent's native tools. The agent should reason through data in its LLM context, not shell out.

3. **Missing error handling.** Each tool-using step should have a fallback: "If this fails, do X instead." Agents without fallbacks get stuck in retry loops.

4. **Ambiguous decision points.** If the agent could interpret a step multiple ways, it will pick the wrong one. Be explicit: "If X, do Y. If Z, do W."

### Step 4: Test the fix
- Spawn the agent with a representative task
- Check the audit trail: did it complete? Did it use the right tools? Did it time out?
- If still failing, go back to Step 2 and look for the next issue

## Common Pitfalls

### Pitfall: `exec` in tools_allowed causes cascading timeouts
**Pattern:** Agent YAML has `tools_allowed: [..., exec]`. Agent uses exec to call a CLI tool or run a script. The subprocess hangs or takes too long. The agent retries. Timeout.
**Fix:** Remove `exec`, add to `tools_denied`. Rewrite instructions to use native tools or in-reasoning processing.

### Pitfall: Tool name mismatch between YAML and engine
**Pattern:** YAML references a tool name that doesn't exist in the engine's registry (renamed, removed, or typo). Agent calls it, gets an error, retries repeatedly, times out.
**Fix:** Verify every tool in `tools_allowed` exists. Check the actual tool name (not what the YAML says). Common renames: contacts→people, merge_contacts→merge_people.

### Pitfall: False tool-gap diagnosis (accepting a diagnosis without verification)
**Pattern:** Another agent or diagnostic report claims "tool X is missing from tools_allowed" as the root cause of a failure. The diagnosing agent didn't actually read the YAML — it inferred the gap from failure behavior. The fix it proposes (add the tool) is wrong because the tool is already there.
**Example:** Architect diagnosed benchmark failure as "write_file missing from devops-analyst YAML" — but write_file was already in tools_allowed. Real cause: benchmark runs didn't pre-stage the input data files (`/tmp/devops_*.json`) the agent tried to read.
**Fix:** ALWAYS read the actual YAML config yourself before accepting any tool-gap diagnosis. Never take another agent's word for what's in the config. The error chain `read_file fails → agent crashes → architect says "add write_file"` is a classic false inference. Read the file, confirm the gap, THEN fix.

### Pitfall: Instructions assume shell access for data processing
**Pattern:** Instructions contain "run this Python script" or "execute this SQL query" blocks. Agent tries exec, fails (if denied) or hangs (if allowed).
**Fix:** Replace with in-reasoning analysis. The agent's LLM can process data in context — tell it to use list_people/list_tasks and reason through the results directly.

### Pitfall: gog CLI for email replies
**Pattern:** Instructions reference `gog gmail send --reply-all` for email replies. Known bug: silently resolves zero recipients, sends to nobody.
**Fix:** ALWAYS use `gws_gmail_reply(thread_id, body)` for replies. `gws_gmail_send(to, subject, body)` for new threads only.

### Pitfall: No backup before editing agent files
**Pattern:** Direct edit of YAML or instruction file without backup. Fix doesn't work; can't revert cleanly.
**Fix:** Always create `.bak-YYYYMMDD` backup before modifying agent config files. Both YAML and instruction files.

## Post-Fix Checklist

- [ ] YAML `tools_allowed` matches actual tool registry names
- [ ] `exec` is in `tools_denied` (unless explicitly needed)
- [ ] Instruction file has no inline code blocks expecting exec
- [ ] Each step has explicit error fallback
- [ ] Email steps use `gws_gmail_reply` / `gws_gmail_send`, never gog CLI
- [ ] Backup files created (.bak-YYYYMMDD)
- [ ] Version and changelog updated in YAML
- [ ] Test spawn completed successfully
