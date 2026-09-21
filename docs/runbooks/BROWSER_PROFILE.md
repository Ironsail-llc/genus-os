# Runbook: the host browser profile

**What this is for.** The `browser` tool's host launch (Playwright Chromium on
the Xvfb display) died on this instance with a message that named the wrong
thing. This runbook records the actual mechanism, the invariant any future edit
must keep, and how to re-verify it in about a minute without a systemd change.

## The failure this exists for

`browser(action="start")` returned:

```
Failed to start browser: BrowserType.launch: Target page, context or browser
has been closed
```

The useful line is in the browser log, which the error truncates away:

```
[pid=…][err] chrome_crashpad_handler: --database is required
[pid=…][err] Try 'chrome_crashpad_handler --help' for more information.
```

**Chromium spawns `chrome_crashpad_handler` with a `--database` path derived from
`$HOME/.config`.** The unit sets `ProtectHome=read-only`, and its
`ReadWritePaths=` allowlist covers only the workspace and a few named config
dirs — `$HOME/.config` itself is not on it. The crashpad handler cannot create
its database, exits immediately, and takes the browser down with it before any
page can load.

Two framings that were tried and are **not** the mechanism, so a future reader
does not chase them again:

| Framing | Why it is wrong |
|---|---|
| "The profile dir `$HOME/.config/google-chrome-for-testing` is read-only (EROFS)" | Playwright manages its **own** temp `--user-data-dir` (`/tmp/playwright_chromiumdev_profile-*`) for a non-persistent `launch()`, so that path is not the one that matters. The failing path is crashpad's database. |
| "Pass `--user-data-dir` to fix it" | Playwright **rejects** it: `Pass user_data_dir parameter to 'browser_type.launch_persistent_context(user_data_dir, **kwargs)' instead of specifying '--user-data-dir' argument`. The launch fails on the argument, before Chromium is even reached. |

## The fix

One thing: **redirect `XDG_CONFIG_HOME` into the workspace**, which
`_browser_env()` does, targeting `_browser_profile_dir(agent_id)`.

The launch args are unchanged. `--user-data-dir` is deliberately not passed.

## THE INVARIANT

> **A browser profile path must live inside the engine unit's `ReadWritePaths=`.**

Moving `_browser_profile_dir()` to `$HOME`, to `/tmp` outside the private tmp,
or anywhere else off that list reintroduces exactly this failure. The profile
lands at `<workspace>/local/browser-profiles/<agent_id>`; the workspace is on
the allowlist, which is why this works.

`ROBOTHOR_WORKSPACE` resolves the root, falling back to `~/robothor`
(`robothor/settings/sources.py`). If the unit ever sets `ROBOTHOR_WORKSPACE` to a
path that is *not* in `ReadWritePaths=`, this fix silently stops working — check
the drop-in before changing the workspace root.

### Per agent, never shared

A shared *writable* profile would let the first agent to authenticate log in
every other agent — the same reasoning as `exec_env._empty_config_dir`. A spawned
sub-agent carries its own id, so it gets its own profile and inherits no cookies.

### Persisted, not per-run

Cookies surviving is the point of a profile. A directory per `exec` on a box
doing hundreds a day is a leak. Retention is left to the operator rather than
guessed at here.

## Verifying it

The unit's hardening can be reproduced without systemd, as a non-root child: a
fake `HOME` whose `.config` is mode `555`. Chromium's crashpad database is
derived from `$HOME/.config`, so this reproduces the failure exactly.

```bash
# bare env (the pre-fix shape) -> FAILS with the crashpad message
# _browser_env (the fix)       -> SUCCEEDS, loads a page
venv/bin/python /tmp/test_xdg_fix.py
```

Pin `PLAYWRIGHT_BROWSERS_PATH` in the **parent** when faking `HOME`:
Playwright's driver resolves the browser executable from its own `HOME`, so
mutating `HOME` in the parent makes it report `Executable doesn't exist at
<fake-home>/.cache/ms-playwright/...` — a false failure that has nothing to do
with the fix.

End-to-end through the real handler, and the two tests that pin the contract:

```bash
venv/bin/python -m pytest \
  robothor/engine/tests/test_browser.py \
  robothor/engine/tests/test_desktop_tools.py \
  robothor/engine/tests/test_desktop_sandbox_consume.py -q
```

`test_host_launch_passes_the_workspace_profile` asserts `XDG_CONFIG_HOME` is a
directory inside the workspace **and that `--user-data-dir` is absent** — the
second assertion is what stops the rejected argument coming back.

### What success looks like

A real launch under a read-only `$HOME/.config` leaves this behind:

```
<workspace>/local/browser-profiles/<agent>/
├── cache/
├── chromium/
│   └── Crash Reports/     <- the crashpad database, now writable
└── ibus/
```

`chromium/Crash Reports/` is the direct evidence: that directory *is* the thing
that could not be created before.

## Credentials in the child environment

`_browser_env()` builds the child env through
`exec_env.build_exec_env(..., base=dict(os.environ), grants=())` instead of
handing Chromium a bare `{**os.environ}` — the ~50 credentials `load-secrets.sh`
decrypts at boot. A page that escaped the renderer sandbox held the fleet's
credentials.

**This is staged, not active.** `ROBOTHOR_EXEC_ENV_MODE` defaults to `observe`,
which returns the environment **unchanged** and only logs what `enforce` would
remove. Today the browser still receives those variables; the removal lands when
the operator promotes the flag on their own evidence. Measured on 2026-09-19:
`observe` → 32 credential-named variables present, `enforce` → 0.

## Deploying it

**The fix is not live until the running deployment carries it.** Check which
tree the engine actually loads:

```bash
systemctl show robothor-engine.service -p ExecStart -p WorkingDirectory
```

On 2026-09-19 this host answered with a **preview deployment** under
`$HOME/.local/share/robothor/combined-preview/<hash>/`, while
`infra/systemd/robothor-engine.service` names `/opt/robothor` — a path that
**does not exist** on this host. Editing the checkout does not change the
running engine. The live copy was still carrying the defect
(`env={**os.environ, "DISPLAY": _display()}`), so a live `browser(action="start")`
keeps failing until the change is deployed.
