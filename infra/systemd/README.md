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
PATH.

Each such script then runs a `require_tools` preflight naming the tools it
cannot answer a question without, and exits non-zero if one is missing — the
unit's own `OnFailure=` then pages, instead of the script reporting on
something it never examined. Optional tools stay optional: `smartctl` in the
volume guard is a gate that says it did not run, and `nvidia-smi` in
`gpu-clock-cap.sh` means there is nothing to cap.

`tests/test_root_scripts_set_path.py` enforces the first half of this for every
script an `EnvironmentFile=` unit starts, deriving the list from the units
themselves so a new one cannot be added without the prelude.
