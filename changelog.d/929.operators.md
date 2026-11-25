The engine's structured log lines (`tool_call`, `llm_call`, `run_end`,
`workflow_step_failed`, `alert`) now end with `tenant_id` and `trace_id`
whenever they are known, and each run's exported trace carries `run.id` and
`tenant.id` as resource attributes. In a log service several tenants share you
can now filter one tenant's runs (for example `event=run_end tenant_id=<tenant>`)
and jump from any line to its trace by `trace_id`. See
[Observability](OBSERVABILITY.md#structured-log-lines) for which lines can lack
which field.
