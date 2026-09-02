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
