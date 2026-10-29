# n8n monitoring

Many operators keep plain automations in [n8n](https://n8n.io): webhooks,
syncs and scheduled jobs that need no agent. Genus OS can watch an external
n8n instance for you. It lists the workflows, records every failed execution,
tells the main agent once per failure, and shows a snapshot on the dashboard
and to the agents.

The monitor is opt-in and off by default. It only reads: it never changes,
starts or stops an n8n workflow.

## Set it up

1. In n8n, create an API key under **Settings > n8n API**. The monitor only
   reads, so give it the least scope your n8n edition allows.
2. Store the key. The vault comes first:

    ```bash
    genus vault set providers/robothor_n8n/api_key
    ```

    `ROBOTHOR_N8N_API_KEY` in the environment file works as well. The vault
    row wins when both are set ([Secrets](../configuration.md#secrets)).

3. Point the instance at n8n and switch the monitor on:

    ```bash
    genus config set ROBOTHOR_N8N_BASE_URL https://n8n.example.com
    genus config set ROBOTHOR_N8N_ENABLED true
    ```

    Use the instance root as the base URL, without `/api/v1`.
4. Restart the engine, then check:

    ```bash
    genus doctor --category integrations
    ```

| Setting | Default | What it does |
|---|---|---|
| `ROBOTHOR_N8N_ENABLED` | `false` | Turns the monitor on. |
| `ROBOTHOR_N8N_BASE_URL` | empty | The n8n instance root. Execution links point into it. |
| `ROBOTHOR_N8N_API_KEY` | empty (secret) | The n8n API key, sent as `X-N8N-API-KEY`. Resolved from the vault first. |
| `ROBOTHOR_N8N_POLL_SECONDS` | `120` | Seconds between polls (minimum 30). Takes effect without a restart. |

The [settings reference](../reference/configuration.md#integrations) is the
generated, complete list.

## What is polled

The engine's scheduler leader runs one poll every `ROBOTHOR_N8N_POLL_SECONDS`.
With HA leader election on, a follower replica makes no n8n call at all, so
two replicas never notify twice. Each round makes these calls:

| Call | Why |
|---|---|
| `GET /healthz` | Is n8n up? |
| `GET /api/v1/workflows` | Every workflow, page by page, 250 per page. Name, active flag, tags and n8n's update time are kept. Node definitions are dropped. |
| `GET /api/v1/executions?status=error` | The newest failed executions, at most 500, without execution data. |
| `GET /api/v1/executions/<id>?includeData=true` | The error message, for new failures only, up to 20 per round. |

The monitor never calls the `projects` or `variables` endpoints. Many n8n
editions refuse them without a licence.

The results land in three tables (migration 156): `n8n_workflows`,
`n8n_executions` and `n8n_sync_state`. A workflow deleted in n8n leaves the
snapshot on the next poll. Failed executions are kept for 30 days. A failure
older than that is never recorded, so it can never come back as new.

## What alerts

**A failed execution.** Each new failure becomes one `workflow_failure`
notification to the main agent, from `n8n`. Its subject is
`n8n: <workflow> failed`. The body holds the error message and a link to the
execution in n8n. The metadata carries `source: n8n`, `workflow_id`,
`execution_id` and `url`. If more than five are waiting in one round, they go
out as ONE summary instead, counted per workflow. The first poll after you
turn the monitor on reports the last 30 days of failures this way.

Each failure is notified once. Its row is stamped `alerted_at` once the
notification is written, and that survives restarts and leader changes. A
notification that could not be written is retried on the next round.

**n8n unreachable.** A round fails when it cannot complete: a rejected key,
the network, a rate limit or a missing setting. The error class (`auth`,
`network`, `rate_limit`, `unknown` or `not_configured`) is recorded as the
sync state's `last_error`. After three failed rounds in a row, ONE `warning`
alert goes out for the outage. A successful round ends the outage, so the
next outage alerts again. A warning reaches the briefing digest, not an
immediate page.

## Where you see it

- **Dashboard.** The bridge serves the snapshot at
  `GET /api/integrations/n8n/workflows` and one workflow's failures at
  `GET /api/integrations/n8n/workflows/{id}/executions?limit=20`. The shape is
  defined in `robothor/integrations/n8n/contract.py`. The status reads
  `not_configured` when the monitor is off, and `unknown` when no poll has
  succeeded in three intervals. It reads `warn` when a workflow failed in the
  last 24 hours or the latest poll failed, and `ok` otherwise.
- **Agents.** `n8n_list_workflows` and `n8n_recent_failures` read the same
  snapshot. They never call n8n ([Tools](../TOOLS.md#n8n-automations-n8n_)).
- **Doctor.** All three checks skip while the monitor is off:
  - `n8n.configured` checks that a base URL and a key are set.
  - `n8n.connection` checks that n8n answers and the key can list a
    workflow. It is skipped under `--offline`.
  - `n8n.poll_freshness` checks that the last good poll is within three
    intervals.
- **Credential test.** `vault_test` on the key's vault row probes
  `GET /api/v1/workflows?limit=1` against the configured base URL. It returns
  `ok` and `1 workflow(s)` or an error class, never the key.

## Limits

- One n8n instance per Genus OS instance.
- Only failed executions are tracked. A run that succeeds but does the
  wrong thing is invisible to the monitor.
- The newest 500 failures per poll are read. If more than 500 pile up
  between two polls, the oldest are missed. Shorten the interval if your
  workflows fail that often.
- The error message is the one n8n recorded, cut at 500 characters. Up to 20
  are read per round, and a failure past that cap shows no message.
- The key is never logged, printed or returned. A failure names only its
  class and HTTP status.
