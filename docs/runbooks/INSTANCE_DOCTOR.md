# Instance Doctor Runbook

`scripts/instance_doctor.sh` answers the question no other command on the box
could: **what is installed here that the repo did not put there?**

`scripts/install-units.sh` and `scripts/install-host-scripts.sh` report
installed / updated / unchanged. That is one direction only — they can say what
the repo pushed onto the box, never what is on the box that no template
describes. Everything in the list below was live on the first Genus OS instance
with nothing anywhere that would print it: a timer symlinked into the repo
checkout, nine live `robothor-*` units with no template at all (two of them
active), twelve `.bak-*` files in a drop-in directory systemd ignores, a
service enabled but not running, and flags set in both
`/etc/robothor/robothor.env` and a drop-in's `Environment=`.

The doctor is **read-only** and exits `0` (clean) or `1` (findings). It
delegates every comparison to `scripts/check_dropin_drift.sh`, which renders
templates through `scripts/render-unit.sh` first, so a placeholder-bearing
mirror is never mistaken for drift.

```bash
scripts/instance_doctor.sh                 # the live box
scripts/instance_doctor.sh --root /tmp/x   # a staged root, for tests
```

## Who runs it, and what a finding costs you

`scripts/guardrail_watch.py` runs it in its **database-free block**
(`check_instance_doctor`), which executes before any DB check, so a Postgres
outage cannot take it down. The daily `robothor-guardrail-watch.timer` fires at
08:30; any finding makes `guardrail_watch.py` exit non-zero, which fails the
unit, which fires `OnFailure=robothor-alert@%n.service` and pages the operator.

So: **a finding is a page.** That is deliberate — every class below was
invisible for months — but it means a finding you do not intend to fix is a
page you will get every morning. Fix it, template it, or (for exactly one
class) allow-list it.

## Finding classes

| Class | Means | Reconcile with |
|---|---|---|
| `not-installed` | a template exists, nothing is installed | `scripts/install-units.sh` / `scripts/install-host-scripts.sh` |
| `template-drift` | the live unit differs from its rendered template | commit the live change to the mirror, then reinstall |
| `host-script-drift` | an installed `/usr/local/bin` copy differs from its repo source | `scripts/install-host-scripts.sh` |
| `cannot-compare` | the comparison **did not happen** — the renderer is missing, or the render env is unresolvable | fix `ROBOTHOR_WORKSPACE` / `ROBOTHOR_SERVICE_USER` (or `/etc/robothor/robothor.env`); this is not drift, and nothing about these units is currently being checked |
| `no-template` | a live unit with no template in `infra/systemd/` | template it, or allow-list it (below) |
| `unmirrored-dropin` | a hand-written drop-in with no repo mirror — unversioned production config | mirror it into `infra/systemd/` |
| `inert-file` | a non-`.conf` file in a drop-in directory; systemd ignores it, a human reading the directory does not | delete it |
| `symlink` | a unit is a symlink, so it was never rendered and moving the target silently unschedules it | reinstall it properly |
| `enabled-not-active` | it is meant to be running and is not | investigate, then `systemctl start` |
| `active-not-enabled` | it is running but disappears at the next reboot | `systemctl enable` |
| `env-shadow` | a key set in both `robothor.env` and a drop-in's `Environment=` — the env file wins, so flipping the drop-in does nothing | keep each flag in exactly one place; see [`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md) |

A unit `systemctl mask`ed to `/dev/null` is an operator decision, not drift,
and is never reported. Under `--root` with no `ROBOTHOR_SYSTEMCTL` seam, the
enabled/active state is reported as `unknown` and the enabled-vs-active check
is **skipped, not silently passed** — the host's systemd has never heard of a
unit staged into a temp directory, and answering "inactive" for it would be a
fabricated fact.

## The allow file — and what it cannot do

`/etc/robothor/instance-units.allow` (override with `--allow-file`) names units
and drop-ins that are deliberately instance-only: a desktop session, a vendor
CRM. It is instance-land by construction — naming those units in the repo
would be exactly the instance data CLAUDE.md rule 1 forbids — so it lives next
to `robothor.env` and is not in git.

Format: one name per line, `#` comments and blank lines ignored. An entry is
either a unit file name (`robothor-x.service`) or a drop-in path
(`robothor-x.service.d/y.conf`).

**It suppresses exactly one class: `no-template` / `unmirrored-dropin`.**

It is not a mute button. A unit named here is still reported for
`template-drift`, `cannot-compare`, `inert-file`, `symlink`,
`enabled-not-active`, `active-not-enabled` and `env-shadow` — those are wrong
whether or not the unit is deliberately instance-only, and a pinned test
(`test_the_allow_file_cannot_suppress_drift_inert_files_or_symlinks`) keeps it
that way. If you find yourself wanting to allow-list your way out of a drift
finding, the answer is to reconcile the drift.

Two things it says out loud on **stderr** (neither is a finding, neither
changes the exit code):

- **the file exists but cannot be read** — every entry is being ignored, so the
  morning page looks exactly like a box that suddenly grew a dozen untemplated
  units. Check its mode and owner.
- **an entry matched nothing** — the unit is gone, or it finally got a
  template. The line reads as coverage and covers nothing; delete it.

## Related

- [`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md) — the drop-in the `env-shadow`
  class is usually about, and the flip procedure that keeps it in sync.
- `scripts/check_dropin_drift.sh` — the comparison the doctor delegates to
  (exit 0 in sync, 1 drift, 2 cannot compare).
- `scripts/gen_cron_map.py` — the same question for schedules rather than units.
