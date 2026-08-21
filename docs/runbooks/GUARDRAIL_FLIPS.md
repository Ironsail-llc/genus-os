# Guardrail Flip Runbook

The engine's guardrail/feature-flag posture lives in a systemd drop-in:

- **Live**: `/etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf`
- **Mirror (source of truth for review/audit)**: `infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf`

`scripts/check_dropin_drift.sh` compares the two (exit 0 in sync, 1 drift, 2 missing).
The daily guardrail-watch report runs it, so unversioned live edits surface within 24h.

## Flip procedure (observe → enforce, or any mode/flag change)

1. **PR first**: edit the *mirror* in a branch, get it reviewed and merged.
   The PR body should link the soak evidence (guardrail-watch output /
   `agent_guardrail_events` counts for the flag's window).
2. **Apply to live** (operator or ops agent on the box):

   ```bash
   sudo cp /etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf \
        /etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf.bak-$(date +%Y%m%d-%H%M%S)
   sudo cp infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf \
        /etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf
   sudo systemctl daemon-reload && sudo systemctl restart robothor-engine
   scripts/check_dropin_drift.sh   # must print OK
   ```

3. **Watch**: one flip per 24h. After each flip confirm in the daily
   guardrail-watch report (or ad hoc `python scripts/guardrail_watch.py`):
   - the flag's events now show the new mode;
   - error+timeout rate stays within the ~2.8–3.9% baseline;
   - no unexpected `blocked` storm from the flipped guardrail.

4. **Probe**: exercise one deliberate violation (e.g. a disallowed exec for
   `exec_allowlist_strict`) and confirm a `blocked` event lands in
   `agent_guardrail_events`.

## ⚠️ The drop-in is not the only source

`/etc/robothor/robothor.env` is loaded via `EnvironmentFile=` and systemd
applies it *after* the drop-in's `Environment=` directives, so any variable set
in both files is governed by the env file — and `check_dropin_drift.sh` will
still report OK, because it only diffs the drop-in against its mirror.

This bit on 2026-07-25: the RIP 15 revert was applied to the drop-in, the
mirror matched, drift-check printed OK, and the running process kept the old
value because `robothor.env:45` also set it. Five RIP flags were duplicated
that way; four happened to agree, which is why it had never been noticed.

`check_dropin_drift.sh` now fails with a `SHADOWED` verdict listing any
variable present in both files. The env file holds instance data (secrets,
tenant ids) so it cannot be mirrored into the repo — keep each flag in exactly
one place, and prefer the versioned drop-in.

**Before trusting a flip, confirm the running process actually changed:**

```sh
PID=$(systemctl show robothor-engine -p MainPID --value)
tr '\0' '\n' < /proc/$PID/environ | grep YOUR_FLAG
```


## Rollback (< 2 minutes)

```bash
sudo cp /etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf.bak-<STAMP> \
     /etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf
sudo systemctl daemon-reload && sudo systemctl restart robothor-engine
```

Then revert the mirror in a follow-up PR so drift-check stays green — never
leave live and mirror disagreeing.

## Current promotion ladder (2026-07-13)

| Flag | Mode | Status |
|------|------|--------|
| `ROBOTHOR_RBAC_MODE` | enforce | done 2026-07-02 |
| `ROBOTHOR_INJECTION_SCAN_MODE` | observe → enforce | 0 events in 11d — flip #1 |
| `ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE` | observe → enforce | 0 events in 11d — flip #2 |
| `ROBOTHOR_APPROVAL_MODE` | observe → enforce | 0 events in 11d — flip #3, on a watched day (see `docs/runbooks/approval-enforce.md`) |
| `ROBOTHOR_COMPLETION_CONTRACTS_MODE` | observe → alert | 3 events — review, then 7-day ladder |
| `ROBOTHOR_RIP_7_MODE`, `ROBOTHOR_RIP_13_MODE` | observe → alert | ladder overdue |
| `ROBOTHOR_SANDBOX_DEFAULT_MODE` | observe | 363 would-blocks — triage + canary before any flip |
| `ROBOTHOR_RUN_VERIFICATION_MODE` | observe | new 2026-08-21 — soak the `unverified_claims` rate before alert |

### `ROBOTHOR_RUN_VERIFICATION_MODE` — what each rung does

Run-level claim verification (`robothor/engine/run_verification.py`) compares a
finished run's claims against the tools that actually succeeded in its own
trace. Unlike `ROBOTHOR_COMPLETION_CONTRACTS_MODE` it needs no session goal and
is not limited to "task complete" phrasings.

| Rung | Behaviour |
|------|-----------|
| `off` | never computed |
| `observe` | verdict stamped on `agent_runs.verified_status` / `.verification`, one `agent_guardrail_events` row per non-`no_claims` verdict, a note appended to `outcome_notes`. Nothing else changes. |
| `alert` | observe + `notify_guardrail_alert` to the operator on any non-`verified` verdict |
| `enforce` | records exactly as `alert` today — gating delivery/task resolution on the verdict is a follow-up PR, and this rung must not be promoted until that lands |

Soak query:

```sql
SELECT verified_status, count(*)
FROM agent_runs
WHERE created_at > now() - interval '7 days' AND verified_status IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

SELECT agent_id, verification->>'unsupported' AS kinds, left(output_text, 120)
FROM agent_runs
WHERE verified_status = 'unverified_claims'
ORDER BY created_at DESC LIMIT 20;
```

Expect a low single-digit percentage. A 400-run replay of recent production
traffic (2026-08-21) produced 387 `no_claims`, 6 `verified`, 7
`unverified_claims` — 1.75%, and the run that motivated the control is among
the 7. The dominant residual class is briefing/summary agents reporting work a
*different* run performed; that is signal, not a bug, but it is the thing to
triage before promoting.
