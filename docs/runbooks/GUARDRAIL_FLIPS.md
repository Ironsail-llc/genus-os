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
