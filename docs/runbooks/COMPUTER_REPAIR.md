# Computer tools and self-repair

The shared engine retains its systemd restrictions. A separate
`robothor-host-exec.service`, running as the instance account, executes verified
owner requests for the main agent using that account's host and sudo permissions.
Workers, child runs, benchmark runs, and plan-only runs cannot obtain host tokens.

Enable routing with `ROBOTHOR_HOST_EXEC_SOCKET=/run/robothor-host/exec.sock` on the
engine. Install the rendered host-exec service with the same instance environment
and a release override pointing to the immutable deployed snapshot. The socket
lives in a mode-0700 directory. Requests use short-lived, audience-bound,
body-bound JWTs and replay protection. No commands or credential values are logged.
The ordinary exec interface is unchanged. A service outage returns a specific
host-execution error; it does not silently change execution backends.

## Deployment

Main can call `exec` with `genus-host deploy <git revision>`. The revision must
contain the current `combined_commit` in `/run/robothor/local-deployment.json`;
merge that commit first if another developer has deployed newer work. The helper
holds `/run/robothor/local-deployment.lock` across deployment and rollback.
It tests the exact snapshot, waits for idle, updates engine/host-service overrides,
restarts, and probes the loaded browser, desktop, and host service. A failed probe
restores both overrides. Other services' release overrides remain intact.

The returned job ID has durable state under `local/repairs/<id>/state.json`.
Use `genus-host status <id>` to distinguish queued, prepared, tested, deployed,
verified, and failed states. Editing a checkout or passing unit tests alone is
not a live repair. A verified job with an execution continuation re-resolves its
original owner and resumes the original request exactly once by correlation ID.
Its result stays in the job and run records; it sends no independent notification.
Plan-only jobs do not resume as execution. External actions must be checked for
prior completion before retrying.

## Browser state

`local/browser-profiles/<scope>/` is writable Chromium XDG config/crash/cache
state, scoped by tenant, principal, and agent. It is **not** a persistent cookie
profile. Playwright owns temporary browser profiles; protected authenticated
sessions use the autonomy workflow service. Never pass `--user-data-dir` to
Playwright's ordinary `launch()`.

Browser/desktop subprocesses receive a minimal environment independently of the
fleet exec-environment rollout flag. Keep DISPLAY and any required XAUTHORITY.
The browser error reports its actual backend and crashpad cause. Container tools
use the configured runtime and cannot fall back to the host if a container fails.

POST `/api/admin/capabilities/probe` with engine-control authorization runs a
non-purchase browser form and screenshot check **inside the live engine**. It
returns the loaded module path and capability evidence, not screenshots or
credentials. The protected autonomy browser has its own readiness check and must
also be checked when changing its deployment.

## Conversation continuity

The current request, immediate conversational context, mode, and subsequent
steering live in a protected engine record inside the system message. They are
checkpointed and survive deterministic shrinking and failed LLM compaction.
Verification uses the current run's task, not the first historical user turn.
A separate plan-alignment check permits one correction before withholding approval.
Telegram revisions have new IDs, hashes, and durable state before approval buttons
are shown, so an old button cannot approve a newer plan.

Approval transitions require a successful durable write before execution starts.
Automatic checkpoint recovery re-resolves the original caller against current
identity records, within the same tenant and agent, before restoring host access.
