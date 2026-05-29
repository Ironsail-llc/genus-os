# Connectors — wrapping external APIs as MCP tools

Genus OS talks to external services through **MCP**. Some services ship a native
MCP endpoint; most expose only a REST/JSON API. The **generic REST→MCP bridge**
(`robothor/connectors/rest_mcp_bridge.py`) closes that gap: it wraps any
HTTP/JSON API (API Platform / Hydra, OpenAPI, plain REST) as a small set of MCP
tools over **stdio**, configured entirely by environment variables.

One bridge binary serves **both** MCP consumers:

| Consumer | stdio framing | How it's configured |
|----------|---------------|---------------------|
| **Claude Code** (CLI on the workstation) | newline-delimited JSON | `claude mcp add` (user or project scope) |
| **RoboThor engine** | Content-Length framing | a business-adapter YAML in `~/.config/robothor/adapters/` |

The bridge **auto-detects** which framing a client uses on the first message and
replies in kind, so the same process works for either. No `mcp` SDK dependency —
the JSON-RPC 2.0 server is hand-rolled (protocol `2024-11-05`).

> **When to use a bridge vs. a native MCP adapter:** if the service already
> exposes an MCP endpoint, point a `transport: http` adapter straight at it (see
> `robothor/engine/adapters.py`). Only use this bridge when the service is
> REST-only. Tip: probe `/.well-known/mcp` and `/mcp` before assuming MCP exists
> — a scope named `mcp:...` on an API token does **not** imply an MCP server.

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CONNECTOR_BASE_URL` | — (required) | API origin, e.g. `https://app.example.com` |
| `CONNECTOR_TOKEN` | — | API token; sent as the auth header, never logged |
| `CONNECTOR_AUTH_HEADER` | `Authorization` | Header to carry the credential |
| `CONNECTOR_AUTH_SCHEME` | `Bearer` | Prefix; set empty for raw `X-API-Key`-style headers |
| `CONNECTOR_TOOL_PREFIX` | `api` | Namespaces tool names (`<prefix>_list`, …) so connectors coexist |
| `CONNECTOR_READONLY` | `1` | `1` = GET-only; `0` also registers create/update/delete |
| `CONNECTOR_ACCEPT` | `application/ld+json` | `Accept` header (Hydra default; use `application/json` for plain REST) |
| `CONNECTOR_API_ROOT` | `/api` | Collection/entrypoint path |
| `CONNECTOR_ALLOWED_RESOURCES` | (all) | CSV allowlist of resource names |
| `CONNECTOR_MAX_CHARS` | `6000` | Response payload cap (truncates with a marker) |
| `CONNECTOR_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `CONNECTOR_NAME` | `rest-connector` | Server label (logs / `serverInfo`) |
| `CONNECTOR_LOG_FILE` | (stderr) | Audit-log file. **Set this for the engine** — RoboThor pipes but does not drain stderr, so a busy server logging to stderr could block. |
| `CONNECTOR_LOG_LEVEL` | `WARNING` | `INFO` to record one `METHOD path -> status` audit line per call |

## Auto-refreshing tokens (login mode)

Many APIs hand out short-lived tokens, so a static `CONNECTOR_TOKEN` is a
time-bomb. Instead, leave `CONNECTOR_TOKEN` empty and give the bridge login
credentials — it mints a token on first use and **re-logs-in automatically on a
401**, so the integration never silently expires:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CONNECTOR_LOGIN_URL` | — | Login endpoint (POST). Enables login mode when set. |
| `CONNECTOR_LOGIN_USERNAME` | — | Username/email |
| `CONNECTOR_LOGIN_PASSWORD` | — | Password (never logged) |
| `CONNECTOR_LOGIN_USERNAME_FIELD` | `email` | JSON body field for the username |
| `CONNECTOR_LOGIN_PASSWORD_FIELD` | `password` | JSON body field for the password |
| `CONNECTOR_LOGIN_TOKEN_FIELD` | `token` | Field in the login response holding the token |

Prefer a **dedicated service account** over a personal login, and keep the
connector read-only — even a high-privilege account can't write through a
read-only bridge.

## Tools exposed

Read-only (always): `<prefix>_list_resources`, `<prefix>_list`, `<prefix>_get`,
`<prefix>_search`. When `CONNECTOR_READONLY=0`, also `<prefix>_create`,
`<prefix>_update`, `<prefix>_delete`.

## Handling sensitive data (PHI/PII)

- **Run read-only** (`CONNECTOR_READONLY=1`) unless writes are a deliberate,
  reviewed decision.
- **Logs never contain bodies or query values** — only `METHOD path -> status`.
  Error payloads surface HTTP status + the API's own title/description, never
  arbitrary response fields. Oversized responses are truncated.
- **Restrict reach**: scope the RoboThor adapter to specific agents
  (`agents: [main]`), and optionally `CONNECTOR_ALLOWED_RESOURCES`.
- **Long-term memory**: agent *tool results are not ingested* into long-term
  memory — only the user + final-assistant transcript is
  (`robothor/memory/conversation_ingest.py`). So raw records don't reach
  `memory_facts`. The residual path is an assistant *summary* that quotes
  sensitive data in its reply; avoid asking agents to persist such summaries.

## Adding a connector

### 1. Claude Code (workstation)

Keep the token out of any tracked file. Put it in a local env file and reference
it:

```bash
# ~/.config/robothor/connectors.env  (chmod 600, outside any repo)
EXAMPLE_API_URL=https://app.example.com
EXAMPLE_API_TOKEN=...           # the secret lives only here

# register at user scope so it's available in every project on this machine
claude mcp add --scope user example-api \
  -e CONNECTOR_BASE_URL=${EXAMPLE_API_URL} \
  -e CONNECTOR_TOKEN=${EXAMPLE_API_TOKEN} \
  -e CONNECTOR_TOOL_PREFIX=example \
  -e CONNECTOR_READONLY=1 \
  -- /home/<user>/robothor/venv/bin/python -m robothor.connectors.rest_mcp_bridge
```

`${VAR}` is expanded from the shell that launches Claude Code, so swapping the
token is a one-line edit to `connectors.env`. Verify with `claude mcp list`.

### 2. RoboThor engine

Drop an adapter YAML (instance config, gitignored) and put the secrets in SOPS so
they reach the engine's environment:

```yaml
# ~/.config/robothor/adapters/example-api.yaml
name: example-api
transport: stdio
command: ["/home/<user>/robothor/venv/bin/python", "-m", "robothor.connectors.rest_mcp_bridge"]
env:
  CONNECTOR_BASE_URL: "${EXAMPLE_API_URL}"
  CONNECTOR_TOKEN: "${EXAMPLE_API_TOKEN}"
  CONNECTOR_TOOL_PREFIX: "example"
  CONNECTOR_READONLY: "1"
  CONNECTOR_LOG_FILE: "/home/<user>/.config/robothor/logs/example-connector.log"
  CONNECTOR_LOG_LEVEL: "INFO"
agents: ["main"]            # least-privilege; widen deliberately
timeout_seconds: 30
description: "Example API (read-only) via generic REST→MCP bridge"
```

```bash
# add the secrets the adapter's ${...} placeholders resolve from
sops /etc/robothor/secrets.enc.json      # add EXAMPLE_API_URL, EXAMPLE_API_TOKEN
sudo systemctl restart robothor-engine   # adapters load per agent-run; restart to pick up
```

Then add the `example_*` tool names to the agent's `tools_allowed` in
`docs/agents/<agent>.yaml` (the adapter's `agents:` list scopes *which* agents
can see them; the allowlist controls *whether* they're exposed). Tools are
discovered from the bridge's `tools/list` at registration — no per-tool code.

## Verify

```bash
# unit tests
venv/bin/python -m pytest robothor/connectors/tests/ -v

# standalone smoke (no engine): pipe JSON-RPC at the bridge
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"<prefix>_list_resources","arguments":{}}}' \
  | CONNECTOR_BASE_URL=... CONNECTOR_TOKEN=... CONNECTOR_TOOL_PREFIX=<prefix> \
    venv/bin/python -m robothor.connectors.rest_mcp_bridge
```
