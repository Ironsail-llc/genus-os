# infra/systemd — unit templates

Platform systemd unit templates. **Do not hand-copy these to
`/etc/systemd/system` and hand-edit them** — that workflow is how installed
units drifted from the repo for months. Install them with:

```bash
sudo ROBOTHOR_WORKSPACE=/path/to/checkout ROBOTHOR_SERVICE_USER=youruser \
    scripts/install-units.sh
sudo systemctl daemon-reload
```

`scripts/install-units.sh` renders every `robothor-*` unit (and the
`robothor-engine.service.d/` drop-ins) through `scripts/render-unit.sh`,
gates `.service` files on `systemd-analyze verify`, and installs
idempotently. Unset variables are read from `/etc/robothor/robothor.env`.
`delphi-*` units are instance-land (some deliberately tombstoned) and are
never installed by the platform installer.

## Template placeholder convention

Templates are valid unit files that parse and verify as-is. Instance-specific
values use exactly these placeholder spellings, substituted at install time:

| Placeholder | Meaning | Rendered to |
|---|---|---|
| `/opt/robothor` | workspace root (the repo checkout) | `$ROBOTHOR_WORKSPACE` |
| `/home/robothor` | the service user's home | `$ROBOTHOR_SERVICE_HOME` (or the user's passwd entry) |
| `User=robothor` (exact line) | the service account | `$ROBOTHOR_SERVICE_USER` |
| `Group=robothor` (exact line) | the service account's group | `$ROBOTHOR_SERVICE_GROUP` (defaults to `$ROBOTHOR_SERVICE_USER`) |
| `robothor robothor` in the USER/GROUP **columns** of an `infra/tmpfiles/*.conf` row | the service account | `$ROBOTHOR_SERVICE_USER`, via `render-unit.sh --tmpfiles` |
| `su robothor robothor` in a logrotate stanza | the account logrotate rotates as | `$ROBOTHOR_SERVICE_USER` / `$ROBOTHOR_SERVICE_GROUP` |

Rules (enforced by `tests/test_install_units.py`):

- **Never `${ROBOTHOR_*}` in unit directives** — systemd does not expand it;
  `systemd-analyze verify` fails outright on such an ExecStart.
- **Never `%h`** — in a *system* unit `%h` is **/root**, not the service
  user's home (a documented past incident). Use `/home/robothor`.
- **No instance accounts or paths** — `User=` lines may only name `robothor`
  (the placeholder), `postgres`, or `root`; no personal usernames, no
  `/home/<realuser>` paths.

### `infra/tmpfiles/` templates

A `systemd-tmpfiles.d(5)` row is `TYPE PATH MODE USER GROUP AGE ARGUMENT`, so
its account fields are **positional** — there is no `User=` prefix for the
renderer's line-anchored rules to match. Rendering such a file therefore needs
an explicit flag:

```sh
scripts/render-unit.sh --tmpfiles infra/tmpfiles/robothor-restart.conf
```

The flag is never inferred from the path: magic that cannot be tested in
isolation is how this class of bug survives. Running a tmpfiles conf through
the *plain* renderer silently emits the placeholder verbatim, which looks
correct and chowns the runtime directory to an account that may not exist on
the target box.

This was not hypothetical. `infra/tmpfiles/robothor-restart.conf` shipped with
a real operator username in those columns, and every gate passed it: the
renderer could not see them, the leak checker had no pattern for a bare
positional account, and the installer copied the file raw. `PATH` columns
containing `robothor` (e.g. `/run/robothor/...`) are real runtime paths and are
never substituted.

`robothor.env.example` is the template for `/etc/robothor/robothor.env`,
which every service sources via `EnvironmentFile=`.

## `robothor-secrets.service` is the ordering point for decrypted secrets

`/run/robothor/secrets.env` lives on tmpfs, so it has to be decrypted on every
boot, and every consumer loads it as **`EnvironmentFile=-`** — optional. That
optionality is deliberate (an instance may run with no SOPS secrets at all) and
it is also a trap: a service that starts *before* the file exists starts
without its credentials and reports itself healthy.

Until this unit existed, the only thing that wrote that file at boot was the
**engine's** `ExecStartPre`, and nothing ordered anyone else after the engine.
On 2026-09-03 the box rebooted at 02:51, `robothor-bridge` started at 02:51:12
with no secrets file on disk, and the engine came up hours later. The bridge
therefore had no `GENUS_BRIDGE_SSO_SECRET`, `_sso_secret_ok` returned False,
and every `POST /api/auth/sso` was answered 403 — for eight days, until someone
tried to sign in through Cloudflare Access and was bounced back to `/signin`.

So the decrypt is now a unit of its own:

| | |
|---|---|
| `robothor-secrets.service` | `Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=<workspace>/scripts/decrypt-secrets.sh` |
| Consumers | `robothor-engine`, `robothor-bridge`, `robothor-app`, `robothor-orchestrator` declare `Requires=` **and** `After=robothor-secrets.service` |

Four things about that are load-bearing:

- **`Requires=`, not `Wants=`.** A bridge with no shared secret cannot complete
  a single login. Failing to start is more honest than starting broken, and it
  is what makes the dependency visible in `systemctl status`.
- **`After=` as well as `Requires=`.** `Requires=` alone is a pull-in, not an
  ordering: systemd would happily start both in parallel, which is the bug.
- **`ConditionPathExists=/etc/robothor/secrets.enc.json`** on the oneshot. An
  instance with no encrypted secrets file *skips* the unit, and systemd treats
  a condition-skipped dependency as satisfied — so `Requires=` does not block
  those instances. The `EnvironmentFile=-` optional semantics are untouched.
- **The consumers keep their own `ExecStartPre` decrypt.** It is idempotent,
  and `RemainAfterExit=yes` means the oneshot will not re-run on its own — so
  removing the `ExecStartPre` would mean a `systemctl restart robothor-engine`
  after a secret rotation silently kept the old values. Keeping it is also the
  smaller diff and leaves existing restart behaviour unchanged.

The timer-driven oneshots (`robothor-backup-*`, `robothor-slo`, …) also read
`secrets.env`. They are deliberately **not** in the list: they fire minutes to
hours after boot and are ordered by their timers. Ordering them too would be
correct but is a much larger diff; if one of them is ever seen starting inside
the boot window, add the same two lines.

`tests/test_install_units.py` asserts the unit exists, is installed by the core
set, and that each of the four consumers carries both directives.

## `EnvironmentFile=` carries a PATH, so every root script sets its own

`/etc/robothor/robothor.env` is instance-land: this repo ships
`robothor.env.example`, and what a box actually has is whatever its operator
wrote. On the first instance that file sets

```
PATH=<user bins>:/usr/local/bin:/usr/bin:/bin
```

— the operator's own PATH, which begins with user-writable directories
(`~/.local/bin`, `~/.npm-global/bin`) and contains no `/usr/sbin` and no
`/sbin`. Every `robothor-*.service` loads that file, so every unit inherits it,
and most of them run as **root**. Both halves of that are bugs:

- **root must not execute a user-writable binary.** Any of those leading
  directories can be rewritten without privilege; a unit that inherits the
  PATH runs whatever it finds there first.
- **`/usr/sbin` is missing**, which is where `dmsetup`, `cryptsetup`,
  `fsck.ext4`, `smartctl` and `runuser` live. On 2026-09-02 the backup volume
  guard therefore could not run any of them: `dmsetup deps` printed nothing
  because it was never found, the guard reads that *output*, and "the tool is
  absent" arrived as "this mapper is backed by nothing". It called its own live
  mapping a stranger's, refused a heal that works by hand, and paged DOWN.

The instance file cannot be fixed from here, and fixing one box would not fix
the next one. **So every root script started by a unit sets its own PATH,
first, before any external command:**

```sh
export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

One line, identical in every root script — including the ones no unit starts
(`slo_probe.sh`, `restore-drill.sh`), which need it for `runuser`. Set, not extended: appending the system directories to an
inherited PATH still takes the first `dmsetup` it finds, which is the
user-writable one. `/usr/local/*` is in the list because `rclone` and `sops`
live there.

**`ROBOTHOR_EXTRA_PATH` is test-only.** It is a leading directory where a test
suite puts stub binaries, which is how those suites still interpose a fake
`curl`, `systemctl` or `dmsetup` now that these scripts inherit nothing. It is
**never** set in a unit and never in `/etc/robothor/robothor.env` — a unit that
set it would be handing root a directory ahead of `/usr/sbin`, which is the
thing this line exists to prevent. Anything from the workspace venv is called
by absolute path (via `SCRIPT_DIR` or `ROBOTHOR_WORKSPACE`), never found on
PATH. `scripts/flag_audit.py` lists it in `DEBUG_ENV_KEYS`, so if a live
process ever has it set, the audit flags it under `DEBUG-ENV` instead of the
leak going unnoticed.

Each such script then runs a `require_tools` preflight naming the tools it
cannot answer a question without, and exits non-zero if one is missing — the
unit's own `OnFailure=` then pages, instead of the script reporting on
something it never examined. Optional tools stay optional: `smartctl` in the
volume guard is a gate that says it did not run, and `nvidia-smi` in
`gpu-clock-cap.sh` means there is nothing to cap.

`tests/test_root_scripts_set_path.py` enforces the first half of this for every
script an `EnvironmentFile=` unit starts, deriving the list from the units
themselves so a new one cannot be added without the prelude.
