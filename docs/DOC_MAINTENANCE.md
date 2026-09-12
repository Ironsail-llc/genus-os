# Doc Maintenance Checklist

When infrastructure, agents, services, or cron jobs change, update docs as part of the same work — not as a follow-up.

| Change | Update these docs |
|--------|-------------------|
| New systemd service | `SERVICES.md`, `docs/SYSTEM_ARCHITECTURE.md` (tunnel table if port-bearing) |
| New cron job (system) | **Regenerate, do not hand-edit**: `python scripts/gen_cron_map.py > docs/CRON_MAP.md` (it reads the crontab, systemd timers and `agent_schedules`, and marks a missing target `MISSING`; the file is gitignored instance data, which is why the generator writes stdout and never the file). Then `brain/CRON_DESIGN.md` (if architectural), `SERVICES.md` |
| New agent | `robothor agent scaffold <id>`, edit manifest + instruction file per contracts, the instance's fleet table + `brain/AGENTS.md` + `docs/CRON_MAP.md`, `scripts/validate_agents.py` |
| Modified agent config | Agent manifest YAML (update first), then `scripts/validate_agents.py --agent <id>` |
| New MCP/plugin tool | `docs/CONNECTORS.md` + the instance's `brain/AGENTS.md` tool list |
| New Cloudflare route | `infra/tunnel/README.md`, `SERVICES.md` (external access table) |
| New database table | `robothor/migrations/manifest.txt` + `docs/SYSTEM_ARCHITECTURE.md` |
| Federation changes | `docs/FEDERATION.md`, `docs/SYSTEM_ARCHITECTURE.md` (federation section), `SERVICES.md` |
| New interactive mode | the instance's `brain/TOOLS.md` + `brain/AGENTS.md`, `CLAUDE.md` (reading guide), `SERVICES.md` (endpoints) |
| Vault credential changes | `robothor/vault/` + the instance's `brain/TOOLS.md` Vault section, `docs/SYSTEM_ARCHITECTURE.md` (secrets management) |
| Deployment/fix with gotchas | Auto-memory `MEMORY.md` (session-to-session learning) |
| Task-lifecycle change (planner / promoter / todo escalation / autonomy defaults) | `docs/SYSTEM_ARCHITECTURE.md` (Task Lifecycle section), `docs/AGENT_BUILDER.md` (Section 2a), and the instance's `brain/HEARTBEAT.md`, `brain/AGENTS.md`, `brain/SOUL.md`, `brain/memory/autonomy_defaults.md` if budgets shift |
| New off/observe/enforce rollout flag | A runbook in `docs/runbooks/` documenting the ladder + soak procedure (see `GUARDRAIL_FLIPS.md` for the systemd-drop-in-governed pattern, `IDENTITY_ROLLOUT.md` for the plain-env-var pattern); note any cross-flag ordering dependency explicitly |
| New systemd unit template, host ops script, or install-truth finding class (`scripts/instance_doctor.sh`) | `docs/runbooks/INSTANCE_DOCTOR.md` (finding table + allow-file scope), `SERVICES.md` |
| New root script started by a systemd unit, or a change to the shared PATH-reset prelude | `infra/systemd/README.md` ("`EnvironmentFile=` carries a PATH" section) — every such script must discard the inherited PATH and set its own, first, before any external command; `tests/test_root_scripts_set_path.py` derives the list of scripts that must carry the prelude from the units themselves |
| New SLO, or a change to `scripts/slo_probe.sh` (or `scripts/guardrail_watch.py`'s `check_slos()` / `check_db_slos()`) | `docs/runbooks/SLOS.md` (objectives table, budgets table, its own "Responding to a page" section for a new page key), `docs/runbooks/PAGING.md` (SLO pages use the sender's two-argument form, so add the one-line consequence to the "What a page means" table there too, since `consequence_for()` never sees an `slo:*` key) |
| Restore-drill change (`scripts/restore-drill.sh`, `robothor-restore-drill.service`/`.timer`) | `docs/runbooks/RESTORE_DRILL.md` (Automated section, measured baselines table) |
| Identity/data-scoping change (`robothor/identity/`) | `docs/runbooks/IDENTITY_ROLLOUT.md`, `docs/SYSTEM_ARCHITECTURE.md` (Cross-System Identity section) |
| Paging path change (`scripts/send_failure_alert.sh`, `scripts/liveness_probe.sh`, `robothor-alert@.service`, `robothor/engine/alerts.py`) | `docs/runbooks/PAGING.md`. **A new `consequence_for()` case must land in the "What a page means" table in the same PR** — an unmapped key pages `(no consequence mapped — add one in send_failure_alert.sh)` onto the operator's phone. New spool/cooldown env vars go in the variable tables there |
| Backup volume, its guard, or the `ExecCondition=` probe (`scripts/backup-volume-guard.sh`, `scripts/backup-volume-check.sh`, `robothor-backup-volume-guard.*`) | `docs/instance/BACKUP_VOLUME_GUARD.md` (page table, reason table, Knobs) — **a new `HEAL_REASON` string must arrive with its row in the reason table**, since the page's whole triage value is that the operator can look the reason up. Add a dated row to its Incident log for any real drop |
| New/changed last-good marker (`scripts/backup-state.sh` and its callers) | `docs/runbooks/RESTORE_DRILL.md` (marker table), `docs/runbooks/OFFSITE_BACKUP.md`, `docs/runbooks/PAGING.md` (the consequence lines quote these) |
| Offsite/PITR/WAL pipeline change (`scripts/backup-offsite.sh`, `scripts/wal-offsite.sh`, `scripts/wal-archive.sh`, `scripts/pg-basebackup.sh`) | `docs/runbooks/OFFSITE_BACKUP.md`, `docs/runbooks/PITR.md` (degraded-mode table), `docs/runbooks/RESTORE_DRILL.md` |
| Deliverable-contract change (`ROBOTHOR_DELIVERABLE_CONTRACT_MODE` and its verifier) | `docs/runbooks/DELIVERABLE_CONTRACT.md`, `docs/runbooks/GUARDRAIL_FLIPS.md` (promotion ladder row), `infra/flags.yaml` |
| Federation change (parent/child handshake, NATS accounts, peer capabilities) | `docs/runbooks/FEDERATION.md` (the operational side: what a peer can do by default, the two-instance ship gate, suspending a child), `docs/FEDERATION.md`, `docs/SYSTEM_ARCHITECTURE.md`, `SERVICES.md` |
| New or changed service role / RBAC grant | `docs/runbooks/SERVICE_ROLES.md` (roles available, the observe-first rollout) |
| Thermal policy change (`scripts/thermal-guard.sh`, `scripts/thermal-shed.sh`, `scripts/gpu-clock-cap.sh`, `ollama.service.d/thermal-limits.conf`) | `docs/instance/THERMAL.md` — **re-measure and update the measured table**; the numbers there are what the clock cap and shed thresholds are derived from |
| New or renamed CLI verb or flag (the parser in `robothor/cli/`) | **Regenerate**: `python scripts/gen_cli_doc.py`. `tests/test_cli_doc_generated.py` fails until you do. Never hand-edit `docs/reference/cli.md` |
| New or re-described setting (`robothor/settings/model.py`) | **Regenerate**: `python scripts/gen_configuration_doc.py`. `tests/test_configuration_doc_generated.py` fails until you do. Never hand-edit `docs/reference/configuration.md`. If an operator would actually touch it, add the *narrative* to `docs/configuration.md` — the tour, never a variable table |
| Anything that changes how an instance is installed (`robothor/init/`, `robothor/doctor/`, `infra/docker-compose*.yml`, `scripts/load-secrets.sh`) | `docs/quickstart.md`. Its two `<!-- install-gate: -->` blocks are executed verbatim by `.github/workflows/install-gate.yml` on a clean runner — edit a command there and the gate is the proof, so let it run |
| A runbook that only describes one deployment (measured hardware numbers, one box's volumes, an incident log) | `docs/instance/`, with the one-line instance banner and a row in `docs/instance/README.md` — not `docs/runbooks/`, which is what every instance gets |
| New page that should appear on the published site | `mkdocs.yml`: the `exclude_docs` allowlist **and** the nav (`tests/test_docs_site.py` requires both), then `mkdocs build --strict` locally |
| **Any new runbook** | Add its row here, in the same PR. A runbook nothing points at is a runbook nobody opens during the incident it was written for |
