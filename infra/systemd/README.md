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
| `User=robothor` / `Group=robothor` (exact lines) | the service account | `$ROBOTHOR_SERVICE_USER` |

Rules (enforced by `tests/test_install_units.py`):

- **Never `${ROBOTHOR_*}` in unit directives** — systemd does not expand it;
  `systemd-analyze verify` fails outright on such an ExecStart.
- **Never `%h`** — in a *system* unit `%h` is **/root**, not the service
  user's home (a documented past incident). Use `/home/robothor`.
- **No instance accounts or paths** — `User=` lines may only name `robothor`
  (the placeholder), `postgres`, or `root`; no personal usernames, no
  `/home/<realuser>` paths.

`robothor.env.example` is the template for `/etc/robothor/robothor.env`,
which every service sources via `EnvironmentFile=`.
