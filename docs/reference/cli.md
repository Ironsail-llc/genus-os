<!--
GENERATED FILE — do not edit.

Rendered from robothor/cli/_build_parser() by scripts/gen_cli_doc.py.
Add or rename a verb there and re-run the script; the committed file and the
generator output are compared in tests/test_cli_doc_generated.py.
-->

# CLI reference

Every verb the `genus` command accepts, with its flags and their defaults.

`genus` is the documented name. `genusos` and `robothor` are aliases for the
same entry point — existing systemd units, cron entries and older runbooks
invoke them, and they keep working.

Where to start, rather than reading this top to bottom:

| I want to… | Command |
| --- | --- |
| install an instance | `genus init` — and see the [quick start](../quickstart.md) |
| find out why an instance is unhealthy | `genus doctor` |
| repair what a diagnostic can repair | `genus doctor --fix` |
| read or change a setting | `genus config get`, `genus config set`, `genus config explain` |
| apply the schema, or see where it stands | `genus migrate`, `genus migrate --status` |
| get back into a fresh instance headlessly | `genus auth setup-link` |

Settings are documented in the [configuration reference](configuration.md),
not here: a flag belongs to one command, a setting to the whole instance.

33 verbs.

## `genus plugin`

List installed plugins and why any were refused.

Usage: `genus plugin`

## `genus init`

Interactive setup wizard.

Usage: `genus init [--yes] [--docker] [--skip-models] [--skip-db] [--workspace WORKSPACE] [--substrate SUBSTRATE] [--wait-timeout SECONDS] [--image-tag TAG] [--dry-run] [--json] [--offline] [--preset PRESET] [--provider PROVIDER] [--model MODEL] [--secrets-backend SECRETS_BACKEND] [--telegram-token TELEGRAM_TOKEN] [--owner-name OWNER_NAME] [--owner-email OWNER_EMAIL] [--start]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--yes`, `-y` | — | off | Non-interactive mode |
| `--docker` | — | off | Use Docker for infrastructure |
| `--skip-models` | — | off | Skip Ollama model pulling |
| `--skip-db` | — | off | Skip database migration |
| `--workspace` | `WORKSPACE` | — | Workspace dir (default: ~/robothor) |
| `--substrate` | `SUBSTRATE` | — | Where the instance runs: local or compose (systemd and helm are not yet selectable) |
| `--wait-timeout` | `SECONDS` | `180` | How long --substrate compose waits for the stack to answer /ready (default: 180) |
| `--image-tag` | `TAG` | — | Released image tag --substrate compose runs (default: v<this CLI's version>; there is no floating `latest`) |
| `--dry-run` | — | off | Print the plan and write nothing |
| `--json` | — | off | Emit the plan, the step results and the first-run URL as JSON on stdout |
| `--offline` | — | off | Record the provider choice without testing it (no completion is made) |
| `--preset` | `PRESET` | — | Agent catalogue preset to install (see: genus agent catalog) |
| `--provider` | `PROVIDER` | — | Model provider id to configure |
| `--model` | `MODEL` | — | Model id to test and record |
| `--secrets-backend` | `env` \| `file` \| `sops` | — | Where this instance's credentials come from (default: env) |
| `--telegram-token` | `TELEGRAM_TOKEN` | — | Verify and configure a Telegram bot token (optional; nothing asks for one) |
| `--owner-name` | `OWNER_NAME` | — | Operator's name for owner.yaml |
| `--owner-email` | `OWNER_EMAIL` | — | Operator's email for owner.yaml |
| `--start` | — | off | Start the services at the end instead of printing the commands |

## `genus upgrade`

Upgrade platform to latest version.

Usage: `genus upgrade [--dry-run] [--pull] [--skip-migrations]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--dry-run` | — | off | Show what would change |
| `--pull` | — | off | Also run 'git pull --ff-only' (git checkouts only; wheels use pip) |
| `--skip-migrations` | — | off | Skip database migrations |

## `genus migrate`

Run database migrations.

Usage: `genus migrate [--dry-run] [--check] [--status] [--adopt-baseline] [--adopt-through MIGRATION_ID]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--dry-run` | — | off | Print SQL without executing |
| `--check` | — | off | Check if required tables exist |
| `--status` | — | off | Show the canonical migration ledger |
| `--adopt-baseline` | — | off | Adopt history this runner never wrote: record the baseline, plus every migration the legacy .robothor/migrations_applied.yaml names, as applied without executing them. For databases created by the retired initdb snapshot or the retired 'robothor upgrade' glob. |
| `--adopt-through` | `MIGRATION_ID` | — | Adopt every migration up to and including MIGRATION_ID without executing them — name the last migration this database already holds. Implies --adopt-baseline. Use when the ledger's only evidence is the legacy schema_migrations table and no side-ledger survives. |

## `genus snapshot`

Create, verify, restore, and retain instance snapshots.

Usage: `genus snapshot {create,list,verify,restore,prune}`

### `genus snapshot create`

Create an atomic snapshot.

Usage: `genus snapshot create [--repository REPOSITORY] [--output OUTPUT] [--workspace WORKSPACE] [--include-secrets] [--plaintext] [--passphrase-env PASSPHRASE_ENV] [--skip-database] [--skip-workspace] [--force]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--repository` | `REPOSITORY` | — | Snapshot repository directory |
| `--output` | `OUTPUT` | — | Exact output file path |
| `--workspace` | `WORKSPACE` | — | Workspace to snapshot |
| `--include-secrets` | — | off | Include .vault-key and existing federation identity key (requires encryption; never includes env secrets) |
| `--plaintext` | — | off | Explicitly disable encryption (forbidden with --include-secrets) |
| `--passphrase-env` | `PASSPHRASE_ENV` | `GENUS_SNAPSHOT_PASSPHRASE` | Environment variable holding the encryption passphrase |
| `--skip-database` | — | off | Create a workspace-only snapshot |
| `--skip-workspace` | — | off | Create a database-only snapshot |
| `--force` | — | off | Replace the exact --output path atomically |

### `genus snapshot list`

List managed snapshots.

Usage: `genus snapshot list [--repository REPOSITORY]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--repository` | `REPOSITORY` | — | Snapshot repository directory |

### `genus snapshot verify`

Authenticate, checksum, inspect, and compatibility-check a snapshot.

Usage: `genus snapshot verify <snapshot> [--passphrase-env PASSPHRASE_ENV]`

| Argument | Required | Description |
| --- | --- | --- |
| `snapshot` | yes | Snapshot file |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--passphrase-env` | `PASSPHRASE_ENV` | `GENUS_SNAPSHOT_PASSPHRASE` | Environment variable holding the decryption passphrase |

### `genus snapshot restore`

Verify/dry-run by default; restore only with explicit confirmation.

Usage: `genus snapshot restore <snapshot> [--workspace WORKSPACE] [--passphrase-env PASSPHRASE_ENV] [--database-only] [--workspace-only] [--confirm] [--force]`

| Argument | Required | Description |
| --- | --- | --- |
| `snapshot` | yes | Snapshot file |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--workspace` | `WORKSPACE` | — | Target workspace |
| `--passphrase-env` | `PASSPHRASE_ENV` | `GENUS_SNAPSHOT_PASSPHRASE` | Environment variable holding the decryption passphrase |
| `--database-only` | — | off | Restore only PostgreSQL |
| `--workspace-only` | — | off | Restore only workspace state |
| `--confirm` | — | off | Execute the restore instead of a dry run |
| `--force` | — | off | Authorize destructive DB cleaning and workspace replacement |

### `genus snapshot prune`

Apply retention policy (dry-run unless --confirm).

Usage: `genus snapshot prune [--repository REPOSITORY] [--keep KEEP] [--older-than-days OLDER_THAN_DAYS] [--confirm]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--repository` | `REPOSITORY` | — | Snapshot repository directory |
| `--keep` | `KEEP` | `7` | Always keep at least this many newest snapshots |
| `--older-than-days` | `OLDER_THAN_DAYS` | — | Delete only snapshots older than N days |
| `--confirm` | — | off | Delete selected snapshots |

## `genus config`

Read and change settings.

Usage: `genus config {get,set,explain,list,validate,schema}`

### `genus config get`

Print one setting and where it came from.

Usage: `genus config get <name> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `name` | yes | Environment variable name, e.g. ROBOTHOR_ENGINE_PORT |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Machine-readable output |

### `genus config set`

Change one setting.

Usage: `genus config set <name> <value> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `name` | yes | Environment variable name |
| `value` | yes | New value; validated against the declared type |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Machine-readable output |

### `genus config explain`

Everything declared about a setting.

Usage: `genus config explain <name> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `name` | yes | Environment variable name |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Machine-readable output |

### `genus config list`

List settings with their sources.

Usage: `genus config list [--group GROUP] [--changed] [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--group` | `GROUP` | — | Only this group (e.g. engine) |
| `--changed` | — | off | Only settings that are not on their default |
| `--json` | — | off | Machine-readable output |

### `genus config validate`

Validate system configuration and connectivity.

Usage: `genus config validate [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Machine-readable output |

### `genus config schema`

Print the JSON Schema of every declared setting.

Usage: `genus config schema`

## `genus doctor`

Diagnose this instance and optionally repair what can be repaired.

Usage: `genus doctor [--fix] [--dry-run] [--json] [--only ID] [--category C] [--offline] [--timeout S]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--fix` | — | off | Repair the failures that declare themselves repairable (migrations, RBAC seed) |
| `--dry-run` | — | off | With --fix, report what would be repaired |
| `--json` | — | off | Machine-readable output |
| `--only` | `ID` | — | Run one check by id |
| `--category` | `C` | — | Run one category, e.g. database |
| `--offline` | — | off | Make no upstream call; the provider completion check is skipped |
| `--timeout` | `S` | `5.0` | Per-check budget in seconds |

## `genus serve`

Start the API server.

Usage: `genus serve [--host HOST] [--port PORT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--host` | `HOST` | `127.0.0.1` | Bind address |
| `--port` | `PORT` | `9099` | Port |

## `genus mcp`

Start the MCP server (stdio transport).

Usage: `genus mcp`

## `genus goal`

Manage the active long-running session goal.

Usage: `genus goal {set,status,evidence,complete,edit-objective,add-criterion,set-target,remove-target} [--tenant TENANT] [--agent AGENT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (defaults to ROBOTHOR_DEFAULT_TENANT or 'default') |
| `--agent` | `AGENT` | — | Agent ID for a per-agent goal (workspace goal otherwise, owner=main) |

### `genus goal set`

Create the active session goal.

Usage: `genus goal set <objective> [--criteria CRITERIA] [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `objective` | yes | Goal objective (one sentence) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--criteria` | `CRITERIA` | `[]` | Success criterion; repeat to provide an explicit completion contract |
| `--json` | — | off | Output JSON |

### `genus goal status`

Show the active session goal.

Usage: `genus goal status [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Output JSON |

### `genus goal evidence`

Record typed evidence.

Usage: `genus goal evidence [--kind KIND] [--summary SUMMARY] [--reference REFERENCE] [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--kind` | `test_run` \| `commit` \| `ci_run` \| `note` | — | test_run: pytest:passed:N or run UUID; commit: git SHA validated via git cat-file; ci_run: https URL; note: free-form (does not satisfy completion) |
| `--summary` | `SUMMARY` | — | Short evidence summary |
| `--reference` | `REFERENCE` | — | Verifiable reference for this kind |
| `--json` | — | off | Output JSON |

### `genus goal complete`

Mark the active session goal complete.

Usage: `genus goal complete <note> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `note` | yes | Completion note |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Output JSON |

### `genus goal edit-objective`

Replace the goal's objective in place.

Usage: `genus goal edit-objective <objective> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `objective` | yes | New objective text |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off |  |

### `genus goal add-criterion`

Append a success criterion to the goal.

Usage: `genus goal add-criterion <text> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `text` | yes | Criterion text |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off |  |

### `genus goal set-target`

Add or replace a metric target on the goal.

Usage: `genus goal set-target <metric> <target> [--weight WEIGHT] [--window-days WINDOW_DAYS] [--category CATEGORY] [--id TARGET_ID] [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `metric` | yes | Metric name (e.g. benchmark_pass_rate) |
| `target` | yes | Target comparator e.g. ">=0.85" or "<0.05" |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--weight` | `WEIGHT` | `1.0` |  |
| `--window-days` | `WINDOW_DAYS` | `7` |  |
| `--category` | `reach` \| `quality` \| `efficiency` \| `correctness` | `correctness` |  |
| `--id` | `TARGET_ID` | — | Stable id for this target (defaults to metric) |
| `--json` | — | off |  |

### `genus goal remove-target`

Remove a metric target by id.

Usage: `genus goal remove-target <target_id> [--json]`

| Argument | Required | Description |
| --- | --- | --- |
| `target_id` | yes | Target id (typically the metric name) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off |  |

## `genus memory-eval`

Run the memory retrieval benchmark suite.

Usage: `genus memory-eval [--suite SUITE] [--tenant TENANT] [--keep] [--json] [--record] [--triggered-by TRIGGERED_BY]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--suite` | `SUITE` | `docs/benchmarks/memory/suite.yaml` | Path to the eval suite YAML |
| `--tenant` | `TENANT` | — | Isolated eval tenant (default: memory-eval) |
| `--keep` | — | off | Skip cleanup of seeded facts (debugging) |
| `--json` | — | off | Output JSON |
| `--record` | — | off | Write the result to benchmark_results so the fleet grader can see it |
| `--triggered-by` | `TRIGGERED_BY` | `manual` | Provenance recorded alongside the result (e.g. cron) |

## `genus status`

Show system status.

Usage: `genus status`

## `genus start`

Start all Genus OS services.

Usage: `genus start`

## `genus stop`

Stop all Genus OS services.

Usage: `genus stop`

## `genus pipeline`

Run intelligence pipeline (coming in v0.2).

Usage: `genus pipeline [--tier TIER]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tier` | `1` \| `2` \| `3` | `1` | Pipeline tier (1=ingest, 2=analysis, 3=deep) |

## `genus version`

Show version.

Usage: `genus version`

## `genus tui`

Launch the terminal chat interface.

Usage: `genus tui [--url URL] [--session SESSION]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--url` | `URL` | `http://127.0.0.1:18800` | Engine URL |
| `--session` | `SESSION` | — | Session key (auto-generated if omitted) |

## `genus tunnel`

Manage tunnel/ingress config.

Usage: `genus tunnel {generate,status}`

### `genus tunnel generate`

Generate tunnel config from enabled services.

Usage: `genus tunnel generate [--provider PROVIDER] [--domain DOMAIN]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--provider` | `PROVIDER` | — | Provider: cloudflare, caddy (default: from env) |
| `--domain` | `DOMAIN` | — | Domain (default: from env) |

### `genus tunnel status`

Check tunnel connectivity.

Usage: `genus tunnel status`

## `genus vault`

Manage the secret vault.

Usage: `genus vault {init,set,get,list,delete,import-env,export-env,audit}`

### `genus vault init`

Generate vault master key.

Usage: `genus vault init`

### `genus vault set`

Store a secret.

Usage: `genus vault set <key> [value] [--category CATEGORY]`

| Argument | Required | Description |
| --- | --- | --- |
| `key` | yes | Secret key (e.g. telegram/bot_token) |
| `value` | no | Value (prompted if omitted) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--category` | `CATEGORY` | `credential` | Category (default: credential) |

### `genus vault get`

Retrieve a secret.

Usage: `genus vault get <key>`

| Argument | Required | Description |
| --- | --- | --- |
| `key` | yes | Secret key |

### `genus vault list`

List secret keys.

Usage: `genus vault list [--category CATEGORY]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--category` | `CATEGORY` | — | Filter by category |

### `genus vault delete`

Delete a secret.

Usage: `genus vault delete <key>`

| Argument | Required | Description |
| --- | --- | --- |
| `key` | yes | Secret key to delete |

### `genus vault import-env`

Import secrets from .env file.

Usage: `genus vault import-env <file>`

| Argument | Required | Description |
| --- | --- | --- |
| `file` | yes | Path to .env file |

### `genus vault export-env`

Export all secrets as KEY=VALUE.

Usage: `genus vault export-env`

### `genus vault audit`

Audit secret usage across the codebase.

Usage: `genus vault audit`

## `genus skills`

Skill library maintenance.

Usage: `genus skills {migrate-state}`

### `genus skills migrate-state`

Move runtime keys (usage_count, last_used, state) out of tracked meta.json files into gitignored state.json sidecars (idempotent).

Usage: `genus skills migrate-state`

## `genus agent`

Agent management.

Usage: `genus agent {scaffold,list,catalog,install,remove,update,resolve,import,setup,search,publish,bind,unbind}`

### `genus agent scaffold`

Scaffold a new agent.

Usage: `genus agent scaffold <agent_id> [--description DESCRIPTION]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID (kebab-case, e.g., ticket-router) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--description`, `-d` | `DESCRIPTION` | — | One-line description |

### `genus agent list`

List installed agents with source/version.

Usage: `genus agent list`

### `genus agent catalog`

Browse available templates.

Usage: `genus agent catalog [--department DEPARTMENT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--department`, `-d` | `DEPARTMENT` | — | Filter by department |

### `genus agent install`

Install agent from template.

Usage: `genus agent install [source] [--preset PRESET] [--yes] [--set SET]`

| Argument | Required | Description |
| --- | --- | --- |
| `source` | no | Template path or agent ID (omit with --preset) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--preset` | `PRESET` | — | Install a preset group |
| `--yes`, `-y` | — | off | Non-interactive |
| `--set` | `SET` | `[]` | Override variables (key=value) |

### `genus agent remove`

Remove an installed agent.

Usage: `genus agent remove <agent_id> [--archive]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID to remove |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--archive` | — | off | Archive instead of delete |

### `genus agent update`

Update agent from template.

Usage: `genus agent update [agent_id] [--template TEMPLATE]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | no | Agent ID (or all) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--template` | `TEMPLATE` | — | New template path |

### `genus agent resolve`

Preview variable resolution.

Usage: `genus agent resolve <path> [--dry-run] [--set SET]`

| Argument | Required | Description |
| --- | --- | --- |
| `path` | yes | Template bundle path |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--dry-run` | — | on | Preview only |
| `--set` | `SET` | `[]` | Override variables (key=value) |

### `genus agent import`

Reverse-engineer existing agent to template.

Usage: `genus agent import <agent_id> [--output OUTPUT]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID to import |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--output`, `-o` | `OUTPUT` | — | Output directory |

### `genus agent setup`

Interactive onboarding wizard.

Usage: `genus agent setup`

### `genus agent search`

Search the hub for agents.

Usage: `genus agent search [query] [--department DEPARTMENT]`

| Argument | Required | Description |
| --- | --- | --- |
| `query` | no | Search query |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--department`, `-d` | `DEPARTMENT` | — | Filter by department |

### `genus agent publish`

Publish template to hub.

Usage: `genus agent publish <repo_url>`

| Argument | Required | Description |
| --- | --- | --- |
| `repo_url` | yes | GitHub repo URL to publish |

### `genus agent bind`

Bind agent to channel/cron schedule.

Usage: `genus agent bind <agent_id> [--channel CHANNEL] [--cron CRON] [--to TO]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID to bind |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--channel` | `CHANNEL` | — | Delivery channel (e.g. telegram) |
| `--cron` | `CRON` | — | Cron expression (e.g. '0 * * * *') |
| `--to` | `TO` | — | Delivery target (e.g. chat ID) |

### `genus agent unbind`

Clear cron and set delivery to none.

Usage: `genus agent unbind <agent_id>`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID to unbind |

## `genus federation`

Peer-to-peer instance networking.

Usage: `genus federation {init,invite,connect,status,list,export,suspend,remove}`

### `genus federation init`

Initialize instance identity (Ed25519 keypair).

Usage: `genus federation init`

### `genus federation invite`

Generate a connection invite token.

Usage: `genus federation invite [--name NAME] [--relationship RELATIONSHIP] [--ttl TTL]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--name` | `NAME` | — | Display name for the peer |
| `--relationship` | `parent` \| `child` \| `peer` | `peer` | Relationship to the connecting instance |
| `--ttl` | `TTL` | `24` | Token TTL in hours (default 24) |

### `genus federation connect`

Accept a connection invite token.

Usage: `genus federation connect <token> [--trust]`

| Argument | Required | Description |
| --- | --- | --- |
| `token` | yes | Invite token from the peer instance |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--trust` | — | off | Skip signature verification (use for pre-shared tokens on trusted networks) |

### `genus federation status`

Show all connections and their health.

Usage: `genus federation status`

### `genus federation list`

List connected instances.

Usage: `genus federation list`

### `genus federation export`

Expose a capability to a peer.

Usage: `genus federation export <connection> <capability>`

| Argument | Required | Description |
| --- | --- | --- |
| `connection` | yes | Connection ID |
| `capability` | yes | Capability to export |

### `genus federation suspend`

Suspend a connection.

Usage: `genus federation suspend <connection>`

| Argument | Required | Description |
| --- | --- | --- |
| `connection` | yes | Connection ID |

### `genus federation remove`

Disconnect from a peer.

Usage: `genus federation remove <connection>`

| Argument | Required | Description |
| --- | --- | --- |
| `connection` | yes | Connection ID |

## `genus auth`

Manage user accounts and identity.

Usage: `genus auth {bootstrap,grant-binding,grants,revoke-binding,setup-link}`

### `genus auth bootstrap`

Seed the owner.yaml operator as the tenant 'owner' account.

Usage: `genus auth bootstrap [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--json` | — | off | Output JSON |

### `genus auth grant-binding`

Arm a one-shot grant binding an existing account to its next SSO sign-in.

Usage: `genus auth grant-binding [--email EMAIL] [--tenant TENANT] [--ttl TTL] [--reason REASON] [--issuer ISSUER] [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--email` | `EMAIL` | — | Account email to bind |
| `--tenant` | `TENANT` | — | Tenant ID (default: platform default) |
| `--ttl` | `TTL` | `15m` | Grant lifetime, e.g. 45s/15m/2h/1d |
| `--reason` | `REASON` | — | Audit reason |
| `--issuer` | `ISSUER` | — | Pin the grant to one IdP issuer URL (default: any allowlisted IdP) |
| `--json` | — | off | Output JSON |

### `genus auth grants`

List SSO binding grants.

Usage: `genus auth grants [--tenant TENANT] [--all] [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: platform default) |
| `--all` | — | off | Include used/revoked/expired grants |
| `--json` | — | off | Output JSON |

### `genus auth revoke-binding`

Revoke a pending binding grant.

Usage: `genus auth revoke-binding <grant_id> [--tenant TENANT]`

| Argument | Required | Description |
| --- | --- | --- |
| `grant_id` | yes | Grant ID |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: any) |

### `genus auth setup-link`

Mint a fresh first-run /setup link (before an owner account exists).

Usage: `genus auth setup-link [--host HOST] [--ttl TTL] [--json]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--host` | `HOST` | — | Host the browser will reach the dashboard on (default: 127.0.0.1) |
| `--ttl` | `TTL` | — | Link lifetime, e.g. 45s/15m/2h (default: the configured TTL) |
| `--json` | — | off | Output JSON |

## `genus user`

Register/link users into the identity graph (closed-allowlist onboarding).

Usage: `genus user {list,add,link,link-face,set-password,mfa-reset}`

### `genus user list`

List tenant users.

Usage: `genus user list [--tenant TENANT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Filter by tenant (default: all tenants) |

### `genus user add`

Register a new user with full identity linkage.

Usage: `genus user add [--tenant TENANT] [--name NAME] [--role ROLE] [--telegram-id TELEGRAM_ID] [--email EMAIL] [--person-id PERSON_ID] [--create-person]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |
| `--name` | `NAME` | — | Display name |
| `--role` | `ROLE` | — | Role: owner/admin/member/user/viewer/auditor |
| `--telegram-id` | `TELEGRAM_ID` | — | Telegram user id |
| `--email` | `EMAIL` | — | Email address |
| `--person-id` | `PERSON_ID` | — | Link to an existing crm_people row |
| `--create-person` | — | off | Force-create a new crm_people row |

### `genus user link`

Link a Telegram id to a person.

Usage: `genus user link [--telegram-id TELEGRAM_ID] [--tenant TENANT] [--person-id PERSON_ID] [--email EMAIL]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--telegram-id` | `TELEGRAM_ID` | — | Telegram user id |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |
| `--person-id` | `PERSON_ID` | — | Existing crm_people id |
| `--email` | `EMAIL` | — | Look up the existing person by email |

### `genus user link-face`

Upsert a face label -> person binding.

Usage: `genus user link-face [--label LABEL] [--person-id PERSON_ID] [--display-name DISPLAY_NAME] [--tenant TENANT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--label` | `LABEL` | — | Face label |
| `--person-id` | `PERSON_ID` | — | crm_people id |
| `--display-name` | `DISPLAY_NAME` | — | Display name to store (default: derived from the person's CRM first+last name) |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |

### `genus user set-password`

Set a user account's local sign-in password (argon2id).

Usage: `genus user set-password <email> [--tenant TENANT] [--password-stdin]`

| Argument | Required | Description |
| --- | --- | --- |
| `email` | yes | Account email address |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |
| `--password-stdin` | — | off | Read the password from stdin instead of prompting (for automation) |

### `genus user mfa-reset`

Clear a user account's two-factor enrollment (they re-enroll).

Usage: `genus user mfa-reset <email> [--tenant TENANT]`

| Argument | Required | Description |
| --- | --- | --- |
| `email` | yes | Account email address |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |

## `genus export`

Export configuration as a portable bundle.

Usage: `genus export [--tenant TENANT] [--output OUTPUT] [--include-memory]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | `TENANT` | — | Tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |
| `--output` | `OUTPUT` | — | Output directory (default: ./robothor-export-<tenant>) |
| `--include-memory` | — | off | Include memory block contents (opt-in — may contain PII) |

## `genus import`

Import configuration from another agent platform.

Usage: `genus import [platform] [--source SOURCE] [--tenant TENANT]`

| Argument | Required | Description |
| --- | --- | --- |
| `platform` | no | Source platform (default: auto-detect) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--source` | `SOURCE` | — | Source path (file or directory) |
| `--tenant` | `TENANT` | — | Target tenant ID (default: ROBOTHOR_DEFAULT_TENANT) |

## `genus tenant`

Create, list, and inspect tenants.

Usage: `genus tenant {create,list,status}`

### `genus tenant create`

Create a new tenant.

Usage: `genus tenant create <id> [--name NAME] [--telegram-user-id TELEGRAM_USER_ID] [--parent PARENT]`

| Argument | Required | Description |
| --- | --- | --- |
| `id` | yes | Tenant ID (e.g. acme) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--name` | `NAME` | — | Display name (default: tenant ID) |
| `--telegram-user-id` | `TELEGRAM_USER_ID` | — | Bind a Telegram user id to the new tenant |
| `--parent` | `PARENT` | — | Parent tenant ID |

### `genus tenant list`

List all tenants.

Usage: `genus tenant list`

### `genus tenant status`

Show tenant details, memory stats, and recent run counts.

Usage: `genus tenant status <tenant_id>`

| Argument | Required | Description |
| --- | --- | --- |
| `tenant_id` | yes | Tenant ID |

## `genus run`

Run agent with a message (non-interactive).

Usage: `genus run [message] [--agent AGENT] [--model MODEL] [--print] [--json] [--timeout TIMEOUT]`

| Argument | Required | Description |
| --- | --- | --- |
| `message` | no | Task description (reads stdin if omitted) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--agent`, `-a` | `AGENT` | — | Agent ID (default: main) |
| `--model`, `-m` | `MODEL` | — | Model override |
| `--print` | — | off | Print final output only |
| `--json` | — | off | Output as JSON |
| `--timeout` | `TIMEOUT` | `600` | Timeout in seconds |

## `genus chat`

Interactive chat (launches TUI).

Usage: `genus chat`

## `genus agents`

List configured agents (shortcut).

Usage: `genus agents`

## `genus costs`

Show cost breakdown.

Usage: `genus costs [--hours HOURS]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--hours` | `HOURS` | `24` | Lookback hours |

## `genus codex`

Manage Codex subscription provider.

Usage: `genus codex {login,status,doctor,test}`

### `genus codex login`

Log in to Codex with ChatGPT.

Usage: `genus codex login [--with-access-token]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--with-access-token` | — | off | Read CODEX_ACCESS_TOKEN from stdin via codex login |

### `genus codex status`

Show Codex login status.

Usage: `genus codex status`

### `genus codex doctor`

Validate ChatGPT subscription auth for codex/* models.

Usage: `genus codex doctor`

### `genus codex test`

Run a small codex/* provider call.

Usage: `genus codex test [prompt] [--model MODEL] [--timeout TIMEOUT]`

| Argument | Required | Description |
| --- | --- | --- |
| `prompt` | no |  |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--model` | `MODEL` | `codex/gpt-5.5` |  |
| `--timeout` | `TIMEOUT` | `120` |  |

## `genus engine`

Manage the agent engine.

Usage: `genus engine {run,start,stop,status,list,history,workflow}`

### `genus engine run`

Run a single agent.

Usage: `genus engine run <agent_id> [--message MESSAGE] [--trigger TRIGGER] [--deep]`

| Argument | Required | Description |
| --- | --- | --- |
| `agent_id` | yes | Agent ID (from YAML manifest) |

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--message`, `-m` | `MESSAGE` | — | User message (default: cron payload) |
| `--trigger` | `TRIGGER` | `manual` | Trigger type |
| `--deep` | — | off | Use deep reasoning (RLM) instead of the normal agent loop |

### `genus engine start`

Start the engine daemon.

Usage: `genus engine start`

### `genus engine stop`

Stop the engine daemon.

Usage: `genus engine stop`

### `genus engine status`

Show engine status.

Usage: `genus engine status`

### `genus engine list`

List configured agents.

Usage: `genus engine list`

### `genus engine history`

Show recent agent runs.

Usage: `genus engine history [--agent AGENT] [--limit LIMIT]`

| Flag | Takes | Default | Description |
| --- | --- | --- | --- |
| `--agent` | `AGENT` | — | Filter by agent ID |
| `--limit` | `LIMIT` | `20` | Max results |

### `genus engine workflow`

Manage workflows.

Usage: `genus engine workflow {list,run,pending,approve,reject}`

## Exit codes

Two commands make promises about their exit code, because scripts gate on them:

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `genus init` | the instance is initialized | a required check failed and **nothing was written** | — |
| `genus doctor` | no `required` check failed | a `required` check failed | the doctor could not run (an unknown `--only` id, a registry that would not import) |

`genus doctor`'s 2 is deliberately separate from its 1: "nothing is wrong" and
"nothing was checked" must never share an exit code, or a typo in a CI gate
becomes a permanently green build.
