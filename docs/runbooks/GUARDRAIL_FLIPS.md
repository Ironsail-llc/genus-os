# Guardrail Flip Runbook

The engine's guardrail/feature-flag posture lives in a systemd drop-in:

- **Live**: `/etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf`
- **Mirror (source of truth for review/audit)**: `infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf`

`scripts/check_dropin_drift.sh` compares the two (exit 0 in sync, 1 drift or
`STALE` backup copies or a `SHADOWED` variable, 2 missing/unresolvable).
The daily guardrail-watch report runs it, so unversioned live edits surface
within 24h.

> **Drift is REPORTED, not PAGED.** `guardrail_watch.check_dropin_drift()`
> prints each comparison's output and returns nothing —
> `scripts/guardrail_watch.py`'s exit code is built from `check_flag_truth`,
> `check_instance_manifests` and `check_instance_doctor` only. So a drifted
> drop-in shows up in the daily report and nowhere else: no non-zero rc, no
> `OnFailure=`, no Telegram. The same is true of
> `check_host_script_drift()`. **Someone has to read the report** — or the
> drift sits in `/etc` indefinitely with every control still green. (The flag
> *audit* is different: see below.)

## Flip procedure (observe → enforce, or any mode/flag change)

1. **PR first**: edit the *mirror* in a branch, get it reviewed and merged.
   The PR body should link the soak evidence (guardrail-watch output /
   `agent_guardrail_events` counts for the flag's window).
2. **Apply to live** (operator or ops agent on the box):

   ```bash
   sudo scripts/install-units.sh          # renders + installs every drop-in
   sudo systemctl daemon-reload && sudo systemctl restart robothor-engine
   scripts/check_dropin_drift.sh   # must print OK
   ```

   **Do not take a dated `.bak-` copy first.** That step is what this
   procedure used to say, and it left twelve `upgrade-rip-flags.conf.bak-*` /
   `.pre-*` files in the live directory going back to 2026-05-30 — none of
   which systemd reads, all of which made the one directory carrying the
   production guardrail posture unreadable at a glance. The rollback source is
   git (see below), which is strictly better: it has the diff, the review and
   the reason. `check_dropin_drift.sh` now reports any such copy as `STALE`
   and exits 1 until it is cleared.

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

`scripts/instance_doctor.sh` reports the same collision as an `env-shadow`
finding over *every* live drop-in, not only the mirrored one — see
[`INSTANCE_DOCTOR.md`](INSTANCE_DOCTOR.md).

`check_dropin_drift.sh` now fails with a `SHADOWED` verdict listing any
variable present in both files. The env file holds instance data (secrets,
tenant ids) so it cannot be mirrored into the repo — keep each flag in exactly
one place, and prefer the versioned drop-in.

**Before trusting a flip, confirm the running process actually changed:**

```sh
PID=$(systemctl show robothor-engine -p MainPID --value)
tr '\0' '\n' < /proc/$PID/environ | grep YOUR_FLAG
```

## `scripts/flag_audit.py` — the whole truth table in one command

The one-flag `grep` above is the manual version of the audit. Run it for every
flag at once, straight from the engine's own `/proc/<MainPID>/environ`:

```sh
python scripts/flag_audit.py          # aligned table; exit 1 on drift
python scripts/flag_audit.py --json   # same data, machine-readable
python scripts/flag_audit.py --no-db  # file layers only, no database needed
```

Per flag it prints the manifest's mode, the drop-in, the env file, the
`feature_flags` DB pin, the **effective** value, and **which layer won** —
plus 7-day evidence rows by action, the last fire, and the last probe. It is
read-only (SELECTs only; it never touches `/etc`, the database or the engine)
and degrades rather than lying: no database prints `?` in the evidence columns
and says so.

Tags:

| Tag | Meaning |
|-----|---------|
| `MISMATCH` | the running process disagrees with `infra/flags.yaml` |
| `SHADOW-LAYER:db` | a `feature_flags` row governs — every file layer is inert |
| `SHADOW-LAYER:envfile` | `robothor.env` governs — a drop-in flip would do nothing, and nothing in git records this posture |
| `SHADOW-LAYER:environ` | the value came from neither file (`systemctl set-environment`, the unit itself, the launching shell) |
| `OVERDUE` | still in a pre-promotion mode past its `planned_promotion` |
| `DEBUG-ENV` | a panic switch or self-test hook is set on this box |

Note the wider `SHADOW-LAYER:envfile` rule: `check_dropin_drift.sh` reports
`SHADOWED` only when a name is in **both** files, so a guardrail living
**only** in `robothor.env` passes it silently — nothing in git says the control
is on, and a rebuilt box would come up without it.

`guardrail_watch` runs this daily (`check_flag_truth`) and **does** exit
non-zero on `MISMATCH`/`SHADOW-LAYER`, so the unit's `OnFailure=` pager fires.
This is the one drift-shaped finding that reaches a phone, and it is worth
being precise about why: `check_flag_truth`'s result is carried into
`main()`'s return value, whereas `check_dropin_drift()` and
`check_host_script_drift()` return `None` and only print. A `SHADOWED` verdict
from `check_dropin_drift.sh` is therefore report-only; the same collision seen
by `flag_audit.py` as `SHADOW-LAYER:envfile` pages.

A non-zero rc from `flag_audit.py` that carries **no table on stdout** is
treated as "the audit could not run" rather than as drift — a missing
`infra/flags.yaml` or a drifted evidence schema used to page as though a
guardrail had moved, which sends the operator to the wrong place.

The fix is always the same: keep each flag in exactly one place — prefer the
versioned drop-in — then update `infra/flags.yaml` to match.


## Rollback (< 2 minutes)

Roll back from git, not from a copy left in `/etc`:

```bash
git -C "$ROBOTHOR_WORKSPACE" log --oneline -- \
    infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf
git -C "$ROBOTHOR_WORKSPACE" checkout <good-sha> -- \
    infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf
sudo scripts/install-units.sh
sudo systemctl daemon-reload && sudo systemctl restart robothor-engine
scripts/check_dropin_drift.sh   # must print OK
```

For a single flag on a governed name, the Controls dashboard is faster still
and needs no restart — it writes a `feature_flags` row that beats every file
layer, and `flag_audit.py` shows it as `PINNED:db@operator:<id>`. Clear the row
when the file layers are back in agreement, or the drop-in stays inert.

Then revert the mirror in a follow-up PR so drift-check stays green — never
leave live and mirror disagreeing, and never leave a `.bak-` copy behind as
"the rollback": git already holds it, with the diff and the reason attached.

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
| `ROBOTHOR_DNC_MODE` | enforce | shipped enforcing — **no ladder**, see below |

### `ROBOTHOR_DNC_MODE` — the one flag with no ladder

The `crm_people.do_not_contact` opt-out
(`robothor/engine/tools/handlers/gws.py::_dnc_refusal`, enforced on
`gws_gmail_send`, `gws_gmail_reply` and `gws_calendar_create`) is a compliance
control, not a containment experiment, so it shipped straight to `enforce` and
has no `planned_promotion`. Two rungs only.

| Rung | Behaviour |
|------|-----------|
| `observe` | the checks run and still file an `agent_guardrail_events` row (`guardrail_name='do_not_contact'`, action `observed`) — **and the message goes out anyway**. The one exception is the unreadable-list branch, which files nothing because that write would go to the database whose failure caused the refusal. |
| `enforce` (default) | the send is refused; the row is filed with action `blocked`. |

There is no `off` — an opt-out that can be switched off entirely is not a
control — and no `alert`, because `observe` already writes the row an alert
rung would page on. `valid_values_for` rejects both, so the dashboard will not
offer them.

**Read the ladder backwards for this one.** Everywhere else in this runbook,
`observe` is a rung on the way up and `enforce` is the goal. Here `enforce` is
the shipped state and `observe` is an incident lever: it exists so an operator
whose CRM is unreachable at 3am has something to reach for other than
commenting out the call, and a guard that is watching and recording beats a
guard that has been deleted. While it is set, people who asked not to be
contacted will be contacted.

So: **if `flag_audit` shows this flag in `observe`, that is an incident to
close, not a soak in progress.** Its evidence verdict is the other exception:
`ENFORCING` needs at least one `do_not_contact` row, so on an instance where
nobody has opted out the flag reads `INERT` indefinitely — that is the guard
having nothing to refuse, not the guard being disconnected; probe it by
flagging a test person and sending to them. It will never appear in the `OVERDUE` list —
`overdue_flags` only nags flags that carry a `planned_promotion`, and this one
deliberately has none. Nothing will remind you. Put the reason in the flip's
`reason` field and set it back.

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

## sandbox_default — read this before promoting it

Measured 2026-08-27. Six agents hold `exec`. **Four of them declare
`sandbox: host`** — `main`, `crm-hygiene`, `conversation-inbox`,
`vision-monitor` — and that opt-out is honoured *before* the mode is consulted.

So promoting `ROBOTHOR_SANDBOX_DEFAULT_MODE=enforce` today would containerise
`auto-agent` and `email-analyst`, and nothing else. The dashboard would read
`enforce` and be telling the truth. The four agents with the broadest host
access would be exactly as uncontained as before.

The would-block set is reassuring for the same reason: opted-out agents never
appear in it, because they never reach the `observe` branch at all.

### Seeing the real scope

```bash
python - <<'PY'
from pathlib import Path
from robothor.engine.config import load_all_manifests
from robothor.engine.sandbox_policy import opted_out_of_containment
print(opted_out_of_containment(load_all_manifests(Path.home() / "robothor" / "docs" / "agents")))
PY
```

The engine also logs each one, once per agent, whenever `sandbox_default` is in
`observe` or `enforce`.

### Making enforce mean enforce

```
Environment=ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST=1
```

Under `observe` this changes no behaviour — opted-out agents still run on the
host — but they now *appear* in the would-block set, so you can see what
`enforce` would newly capture before you flip it. Under `enforce` it
containerises them.

Set it in `observe` first and read the new rows. Containerising `main` without
knowing what it does on the host is a worse outcome than leaving it
uncontained.

An explicit `sandbox: docker` always wins, in every mode. Agents without `exec`
are never in scope, with or without the override.
